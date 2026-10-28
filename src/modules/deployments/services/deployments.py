# Intentions: deploying a project's site -- whatever's in its workspace right now, built and running in
# production on its domains -- and telling where that stands.
#
# A deployment pins the workspace as a git commit, then rolls that commit out with ensure_release: a list of
# steps that each look at what's already there and only do what's missing (the image exists? the container
# runs this commit and answers?...). That's what makes retrying safe: an attempt that died half-way leaves
# the next one less to do, never more. After DEPLOYMENT_ATTEMPTS failures, the commit that was live before
# is rolled out again -- the same ensure_release, an older commit (its image is still on the host).
#
# Nothing about what's live is recorded by us: docker's container labels say which commit runs (see
# containers.py), the workspace's repo says whether anything changed since. The deployment log is the events
# table, append-only -- history only, never read to decide anything. One deployment = one run id (the
# events' grouping); its subject is the project, its direct object the commit.

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from itertools import groupby

import pydash

from src.dto.projects import Project
from src.modules.deployments.constants import (
    DEPLOYMENT_ATTEMPTS,
    DEPLOYMENT_COOLDOWN_SECONDS,
    DEPLOYMENT_ERROR_CHARS,
    DEPLOYMENT_LOCK_TTL_SECONDS,
    DEPLOYMENT_OUTCOMES,
    DEPLOYMENT_RETRY_DELAY_SECONDS,
    DeploymentEvent,
)
from src.modules.deployments.services import containers, environment, images, sites, sources
from src.repositories import events as events_repo
from src.services import devenv
from src.utils import redis
from src.utils.common import spawn
from src.utils.logging import log_error


class DeploymentInProgress(Exception):
    """The project is already being deployed. 409."""


class NothingToDeploy(Exception):
    """The project has no app yet (it hasn't reached building). 409."""


class DeploymentCooldown(Exception):
    """The last deployment started less than DEPLOYMENT_COOLDOWN_SECONDS ago. `until`: when the next can start."""

    def __init__(self, until: datetime):
        super().__init__(f"next deployment possible at {until.isoformat()}")
        self.until = until


async def deploy(project: Project, conversation_id: int, enforce_cooldown: bool = True) -> dict:
    """Deploy what's in the conversation's workspace now: commit it, then roll it out in the background
    (_roll_out). Returns {run_id, commit} as soon as the commit is pinned. Raises NothingToDeploy,
    DeploymentCooldown (only with `enforce_cooldown` -- a user's own deploys; the platform's skip it), or
    DeploymentInProgress while another deployment of the project runs (the lock is held from here until the
    background half finishes)."""
    if not devenv.has_app(project, conversation_id):
        raise NothingToDeploy(f"project {project['id']} has no app to deploy")
    cooldown_until = await get_cooldown_until(project)
    if enforce_cooldown and cooldown_until:
        raise DeploymentCooldown(cooldown_until)
    if not await redis.set_if_absent(_get_lock_key(project["id"]), DEPLOYMENT_LOCK_TTL_SECONDS):
        raise DeploymentInProgress(f"project {project['id']} is being deployed")
    try:
        # The workspace brings older workspaces the template's .gitignore before their first commit.
        await devenv.ensure_workspace(project, conversation_id)
        await devenv.ensure_container(project)
        commit = await sources.commit_workspace(project, conversation_id)
        live = await containers.get_live_release(project["id"])
        run_id = uuid.uuid4().hex
        await _record(DeploymentEvent.STARTED, project, run_id, commit, {"previous_commit": pydash.get(live, "commit")})
    except BaseException:
        await redis.delete(_get_lock_key(project["id"]))
        raise
    spawn(_roll_out(project, conversation_id, run_id, commit, pydash.get(live, "commit")))
    return {"run_id": run_id, "commit": commit}


async def get_deployment_status(project: Project, conversation_id: int) -> dict:
    """Where the project's site stands, all read live:
    live                    {commit, deployed_at, running} from docker, None if never deployed
    deploying               whether a deployment runs right now
    has_undeployed_changes  whether the workspace differs from the live commit; None when we can't tell
                            (the dev container is stopped -- not worth waking for a status)
    can_deploy              there's an app, nothing's deploying, and there's something new to deploy (or
                            we can't tell)
    domains, urls           where the site is served right now (its active domain, if it may be served)
    site_domains            every domain attached to it: [{domain, kind, activated_at, created_at}]
    cooldown_until          when a user may deploy again (None = now)
    last_deployment         the newest entry of list_deployments, None if never deployed"""
    live = await containers.get_live_release(project["id"])
    deploying = await redis.flag_exists(_get_lock_key(project["id"]))
    has_app = devenv.has_app(project, conversation_id)
    has_changes = await sources.has_changes_since(project, conversation_id, pydash.get(live, "commit"))
    deployments = await list_deployments(project)
    domains = await sites.get_served_domains(project)
    cooldown_until = await get_cooldown_until(project)
    return {
        "live": live,
        "deploying": deploying,
        "has_undeployed_changes": has_changes,
        "can_deploy": has_app and not deploying and has_changes is not False,
        "domains": domains,
        "urls": sites.to_site_urls(domains),
        "site_domains": await sites.get_site_domains(project),
        "cooldown_until": cooldown_until,
        "last_deployment": pydash.head(deployments),
    }


async def get_cooldown_until(project: Project) -> datetime | None:
    """When a user may next deploy the project: DEPLOYMENT_COOLDOWN_SECONDS after its last deployment started, or
    None if that's already passed (or it never was)."""
    started = await events_repo.find(
        {"type": DeploymentEvent.STARTED, "subject_type": "project", "subject_ref": str(project["id"])}
    )
    until = pydash.last(started)["created_at"] + timedelta(seconds=DEPLOYMENT_COOLDOWN_SECONDS) if started else None
    return until if until and until > datetime.now(UTC) else None


async def list_deployments(project: Project) -> list[dict]:
    """The project's deployments, newest first, folded from the deployment log (_fold_deployment)."""
    events = await events_repo.find(
        {"subject_type": "project", "subject_ref": str(project["id"]), "grouping_type": "deployment"}
    )
    deploying = await redis.flag_exists(_get_lock_key(project["id"]))
    runs = [list(run_events) for _, run_events in groupby(events, key=lambda event: event["grouping_ref"])]
    return [_fold_deployment(run_events, deploying and index == 0) for index, run_events in enumerate(reversed(runs))]


def _fold_deployment(run_events: list[dict], is_newest_running: bool) -> dict:
    """[Pure] One deployment's events (oldest first) -> {id, commit, started_at, finished_at, outcome,
    live_commit, attempts: [{attempt, error, at}], error}. outcome is the last event's (succeeded /
    rolled_back / failed), "running" while it runs, or "interrupted" when it never finished (the backend
    died mid-way). live_commit is what the deployment left running."""
    started, last = run_events[0], run_events[-1]
    finished = last["type"] in DEPLOYMENT_OUTCOMES
    unfinished_outcome = "running" if is_newest_running else "interrupted"
    return {
        "id": started["grouping_ref"],
        "commit": started["direct_object_ref"],
        "started_at": started["created_at"],
        "finished_at": last["created_at"] if finished else None,
        "outcome": DeploymentEvent(last["type"]).name.lower() if finished else unfinished_outcome,
        "live_commit": last["direct_object_ref"] if last["type"] != DeploymentEvent.FAILED and finished else None,
        "attempts": [
            {
                "attempt": pydash.get(event, "value.attempt"),
                "error": pydash.get(event, "value.error"),
                "at": event["created_at"],
            }
            for event in run_events
            if event["type"] == DeploymentEvent.ATTEMPT_FAILED
        ],
        "error": pydash.get(last, "value.error") if finished else None,
    }


async def ensure_release(project: Project, conversation_id: int, commit: str) -> None:
    """Make sure `commit` is what the project's site runs, on all its domains -- every step skipped when it's
    already done (see the top of this file)."""
    image_tag = images.get_image_tag(project["id"], commit)
    if not await images.image_exists(image_tag):
        await devenv.ensure_container(project)  # the archive comes out of it, and an idle sweep may have stopped it
        await images.build_image(
            project["id"], image_tag, await sources.archive_commit(project, conversation_id, commit)
        )
    await environment.ensure_database_access(project)
    # Read once, so the site's CORS origins and its nginx file agree within an attempt.
    domains = await sites.get_served_domains(project)
    site_env = environment.render_site_env(project, sites.to_site_urls(domains))
    await containers.ensure_site_container(project, image_tag, commit, site_env)
    await sites.ensure_site(project, domains)


async def _roll_out(
    project: Project, conversation_id: int, run_id: str, commit: str, previous_commit: str | None
) -> None:
    """deploy's background half: _release_or_roll_back, then the lock goes."""
    try:
        await _release_or_roll_back(project, conversation_id, run_id, commit, previous_commit)
    finally:
        await redis.delete(_get_lock_key(project["id"]))


async def _release_or_roll_back(
    project: Project, conversation_id: int, run_id: str, commit: str, previous_commit: str | None
) -> None:
    """Roll `commit` out (with retries); if it never comes up, roll `previous_commit` back out (with retries
    too). Every outcome lands in the log, errors in the error log too -- this is where they stop."""
    try:
        await _ensure_release_retrying(project, conversation_id, run_id, commit)
        await _record(DeploymentEvent.SUCCEEDED, project, run_id, commit)
        await images.prune_images(project["id"], images.get_image_tag(project["id"], commit))
        return
    except Exception as error:
        log_error(error, f"deploying project {project['id']} failed", {"run_id": run_id, "commit": commit})
        failure = error
    if not previous_commit or previous_commit == commit:
        await _record(DeploymentEvent.FAILED, project, run_id, commit, {"error": _to_error_text(failure)})
        return
    try:
        await _ensure_release_retrying(project, conversation_id, run_id, previous_commit)
        await _record(DeploymentEvent.ROLLED_BACK, project, run_id, previous_commit, {"error": _to_error_text(failure)})
    except Exception as rollback_error:
        log_error(rollback_error, f"rolling project {project['id']} back failed", {"run_id": run_id})
        await _record(
            DeploymentEvent.FAILED, project, run_id, previous_commit, {"error": _to_error_text(rollback_error)}
        )


async def _ensure_release_retrying(
    project: Project, conversation_id: int, run_id: str, commit: str, attempt: int = 1
) -> None:
    """Recursive: ensure_release, and on a failure log the attempt and go again after a pause -- up to
    DEPLOYMENT_ATTEMPTS, then the last error goes up."""
    try:
        await ensure_release(project, conversation_id, commit)
    except Exception as error:
        await _record(
            DeploymentEvent.ATTEMPT_FAILED,
            project,
            run_id,
            commit,
            {"attempt": attempt, "error": _to_error_text(error)},
        )
        if attempt >= DEPLOYMENT_ATTEMPTS:
            raise
        await asyncio.sleep(DEPLOYMENT_RETRY_DELAY_SECONDS * attempt)
        await _ensure_release_retrying(project, conversation_id, run_id, commit, attempt + 1)


async def _record(
    event_type: DeploymentEvent, project: Project, run_id: str, commit: str, value: dict | None = None
) -> None:
    await events_repo.insert(
        {
            "type": event_type,
            "subject_type": "project",
            "subject_ref": str(project["id"]),
            "direct_object_type": "commit",
            "direct_object_ref": commit,
            "grouping_type": "deployment",
            "grouping_ref": run_id,
            "value": value or {},
        }
    )


def _to_error_text(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"[-DEPLOYMENT_ERROR_CHARS:]


def _get_lock_key(project_id: int) -> str:
    return f"deployment:{project_id}"
