# Intentions: the FastAPI app -- wire up the connections this process needs at startup, mount the
# routers, tear the connections down on shutdown.
#
# STARTUP POLICY: Postgres is REQUIRED (it's the store; booting without it just means every request
# 500s, so fail loudly at startup instead). The schema is NOT applied here -- run scripts/migrate.py. Redis and the config snapshot are BEST-EFFORT -- the app is
# useful without either (nothing caches yet, and settings fall back to code defaults), so a missing
# local redis shouldn't stop you from running the API.

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from src import debug_scratch
from src.config import settings
from src.constants import AGENT_LISTENER_PORT, DEVENV_IDLE_SWEEP_SECONDS, SERVER_GRACEFUL_SHUTDOWN_SECONDS
from src.controllers.agent import listener_router as agent_listener_router
from src.controllers.agent import router as agent_router
from src.controllers.auth import router as auth_router
from src.controllers.conversations import router as conversations_router
from src.controllers.project_folders import router as project_folders_router
from src.controllers.projects import router as projects_router
from src.controllers.system import router as system_router
from src.controllers.system_configurations import router as system_configurations_router
from src.controllers.users import router as users_router
from src.services import devenv, system_config
from src.services import projects as projects_service
from src.utils import http, postgres, redis
from src.utils.logging import configure_logging, log_error

# Every route lives under /api so a host nginx has a single rule (/api/ -> this container) and nothing
# is served bare at the root.
_API_PREFIX = "/api"
_log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # configure_logging runs here, not only in main(): under `uvicorn --reload` the app is imported by
    # a subprocess that never runs main(), and uvicorn's own logging config sets up only the uvicorn*
    # loggers (root gets no handler). Without this our own log lines would silently vanish there.
    # Idempotent.
    configure_logging()

    await postgres.connect()
    await http.connect()

    try:
        await redis.connect()
    except Exception as error:
        log_error(error, "redis connect failed at startup; cache calls will error until it's up")

    # Warm the runtime-settings snapshot so the first request doesn't pay for the load. Best-effort:
    # an empty (or not-yet-migrated) table just means everything falls back to its code default.
    try:
        await system_config.force_load()
    except Exception as error:
        log_error(error, "system config load failed at startup; running on defaults")

    # LOCAL-ONLY scratch hook: runs debug_scratch.do_test() on the live loop with live connections. uvicorn
    # --reload re-runs startup on every save, so this is the "edit do_test, save, see output" debug loop.
    # No-op unless you've put something in do_test; never runs outside local (and it swallows its own errors).
    if settings.env == "local":
        await debug_scratch.run_do_test()

    agent_listener = await _serve_agent_listener()
    idle_sweep = asyncio.create_task(_sweep_idle_projects())

    yield

    idle_sweep.cancel()
    if agent_listener:
        agent_listener.should_exit = True
        await agent_listener.shutdown()
    await http.close()
    await redis.close()
    await postgres.close()


async def _sweep_idle_projects() -> None:
    """Every DEVENV_IDLE_SWEEP_SECONDS, stop the projects nobody has used for a while
    (projects.stop_idle_projects). Every worker runs this loop, but only one does each round: the first to
    claim it in Redis. A round that fails is logged and the next one tries again."""
    while True:
        await asyncio.sleep(DEVENV_IDLE_SWEEP_SECONDS)
        try:
            if await redis.set_if_absent("devenv:idle-sweep", DEVENV_IDLE_SWEEP_SECONDS - 10):
                stopped = await projects_service.stop_idle_projects()
                if stopped:
                    _log.info("idle projects stopped: %s", stopped)
        except Exception as error:
            log_error(error, "the idle sweep failed; the next round tries again")


async def _serve_agent_listener() -> uvicorn.Server | None:
    """Start the agent listener: the agent endpoints -- and only them, the model proxy among them -- for the
    projects' containers, on the containers' gateway (the one host address they can reach; never a public
    interface). Every request still needs a session token. Shut down with the app. Without docker on the
    machine (no containers, so no gateway) there's nothing to listen for: logged and skipped."""
    try:
        await devenv.ensure_network()
    except Exception as error:
        log_error(error, "no projects' network (is docker there?): the agent listener isn't started")
        return None
    listener_app = FastAPI(title=f"{settings.app_name} agents")
    listener_app.include_router(agent_router, prefix=_API_PREFIX)
    listener_app.include_router(agent_listener_router, prefix=_API_PREFIX)
    config = uvicorn.Config(
        listener_app,
        host=devenv.get_gateway_ip(),
        port=AGENT_LISTENER_PORT,
        lifespan="off",
        log_config=None,
        # Model answers stream for minutes: a shutdown mustn't wait them all out (see main.py).
        timeout_graceful_shutdown=SERVER_GRACEFUL_SHUTDOWN_SECONDS,
    )
    config.load()
    server = uvicorn.Server(config)
    # startup/shutdown, not serve(): serve() would take over the process's signal handling from the main
    # server. serve() also sets up the server's lifespan handler, which startup() expects (off here).
    server.lifespan = config.lifespan_class(config)
    await server.startup()
    return server


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    # CORS for the browser FE. The session rides an httpOnly cookie, so credentials ARE allowed -- which
    # means explicit origins only (browsers reject "*" + credentials). Defaults to FRONTEND_URL.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.resolved_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Carries Google login's state/nonce/next/guest_id between /auth/google/login and the callback --
    # nothing else uses request.session. Short-lived, since a login round trip takes seconds. Mounted
    # only with a secret; without one Google login is disabled and never touches the session.
    if settings.session_secret:
        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.session_secret,
            session_cookie="oauth_session",
            max_age=600,
            same_site="lax",
            https_only=settings.env != "local",
        )

    app.include_router(users_router, prefix=_API_PREFIX)
    app.include_router(auth_router, prefix=_API_PREFIX)
    app.include_router(projects_router, prefix=_API_PREFIX)
    app.include_router(project_folders_router, prefix=_API_PREFIX)
    app.include_router(agent_router, prefix=_API_PREFIX)
    app.include_router(conversations_router, prefix=_API_PREFIX)
    app.include_router(system_configurations_router, prefix=_API_PREFIX)
    app.include_router(system_router, prefix=_API_PREFIX)
    return app


app = create_app()
