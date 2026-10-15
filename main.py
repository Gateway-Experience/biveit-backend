# Intentions: the process entry point. Configures logging before anything else, then hands off to
# uvicorn. The app itself is src/app.py.

import uvicorn

from src.config import settings
from src.constants import SERVER_GRACEFUL_SHUTDOWN_SECONDS
from src.utils.logging import configure_logging


def main() -> None:
    configure_logging()
    # The app is passed as an IMPORT STRING (not the object) so uvicorn's reloader can re-import it on
    # a code change. reload is a dev convenience -- it watches the filesystem and spawns a subprocess,
    # so it must stay false in prod (API_RELOAD).
    uvicorn.run(
        "src.app:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.api_reload,
        reload_dirs=["src"] if settings.api_reload else None,
        # Without a cap, a reload or shutdown waits for EVERY open connection to finish -- and SSE streams
        # (GET /conversations/{id}/events) stay open for up to SSE_MAX_AGE_SECONDS, so the server would
        # hang half-restarted. Past the cap they're closed; EventSource reconnects and resumes on its own.
        timeout_graceful_shutdown=SERVER_GRACEFUL_SHUTDOWN_SECONDS,
    )


if __name__ == "__main__":
    main()
