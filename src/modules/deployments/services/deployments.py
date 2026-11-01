# Intentions: deploying a project's site -- whatever's in its workspace right now, built and running in
# production on its domains -- and telling where that stands.
#
# A deployment pins the workspace as a git commit, then rolls that commit out with ensure_release: a list of
# steps that each look at what's already there and only do what's missing (the image exists? the container
# runs this commit and answers?...). That's what makes retrying safe: an attempt that died half-way leaves
# the next one less to do, never more. After DEPLOYMENT_ATTEMPTS failures, the commit that was live before
# is rolled out again -- the same ensure_release, an older commit (its image is still on the host).
#
# A release is a commit on a domain: the site's active one (sites.py), or none before it has one. Setting another
# domain up is the same ensure_release on the live commit, for that domain (activate_domain): no build, and not a
# deployment -- the domain only becomes active once that's worked, so the old one serves until then.
#
# Nothing about what's live is recorded by us: docker's container labels say which commit runs (see
# containers.py), the workspace's repo says whether anything changed since. The deployment log is the events
# table, append-only: history -- never read for what's live, only for how deployments went (get_activation reads a
# first release that failed back from it). One deployment = one run id (the events' grouping); its subject is the
# project, its direct object the commit, and a first release on a domain says which (value.domain). A switch isn't a
# deployment: it's a long-running operation (modules/long_running_ops) its caller polls by id (get_domain_switch) --
# only to tell the user how it went. Which domain is being set up right now is the lock's own value.

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from itertools import groupby
from uuid import UUID

import pydash

from src.dto.projects import Project
from src.modules import long_running_ops
from src.modules.deployments.constants import (
    DEPLOYMENT_ATTEMPTS,
    DEPLOYMENT_COOLDOWN_SECONDS,
    DEPLOYMENT_ERROR_CHARS,
    DEPLOYMENT_LOCK_TTL_SECONDS,
    DEPLOYMENT_OUTCOMES,
    DEPLOYMENT_RETRY_DELAY_SECONDS,
    DOMAIN_SWITCH_DEADLINE_SECONDS,
    DeploymentEvent,
)
from src.modules.deployments.repositories import site_domains as site_domains_repo
from src.modules.deployments.services import containers, environment, images, sites, sources
from src.repositories import events as events_repo
from src.services import devenv
from src.utils import redis
from src.utils.common import spawn
from src.utils.logging import log_error

# The `kind` of activate_domain's long-running operation (its first event's value).
_DOMAIN_SWITCH = "domain_switch"


class DeploymentInProgress(Exception):
    """The project is already being deployed. 409."""


class NothingToDeploy(Exception):
    """The project has no app yet (it hasn't reached building). 409."""


class DeploymentCooldown(Exception):
    """The last deployment started less than DEPLOYMENT_COOLDOWN_SECONDS ago. `until`: when the next can start."""

    def __init__(self, until: datetime):
        super().__init__(f"next deployment possible at {until.isoformat()}")
        self.until = until


async def deploy(
    project: Project, conversation_id: int, enforce_cooldown: bool = True, domain: str | None = None
) -> dict:
    """Deploy what's in the conversation's workspace now: commit it, then roll it out in the background
    (_roll_out), on the site's active domain -- or, with `domain`, on that attached domain, which becomes the active
    one once it's up (the site's first release, see activate_domain). Returns {run_id, commit} as soon as the
    commit is pinned. Raises NothingToDeploy, DeploymentCooldown (only with `enforce_cooldown` -- a user's own
    deploys; the platform's skip it), or DeploymentInProgress while another deployment (or domain setup) of the
    project runs (the lock is held from here until the background half finishes)."""
    if not devenv.has_app(project, conversation_id):
        raise NothingToDeploy(f"project {project['id']} has no app to deploy")
    cooldown_until = await get_cooldown_until(project)
    if enforce_cooldown and cooldown_until:
        raise DeploymentCooldown(cooldown_until)
    # The lock's value: the domain this deployment sets up, if any (get_activation reads it).
    if not await redis.set_if_absent(_get_lock_key(project["id"]), DEPLOYMENT_LOCK_TTL_SECONDS, domain or ""):
        raise DeploymentInProgress(f"project {project['id']} is being deployed")
    try:
        # The workspace brings older workspaces the template's .gitignore before their first commit.
        await devenv.ensure_workspace(project, conversation_id)
        # Need for commit_workspace (running git insider the dev container).
        await devenv.ensure_container(project)
        # If there's no new changes, the commit is still returned but subsequent steps mostly returns quickly.
        commit = await sources.commit_workspace(project, conversation_id)
        # Currently deployed release. So `commit` is usually newer than live.commit.
        live = await containers.get_live_release(project["id"])
        run_id = uuid.uuid4().hex
        await _record(
            DeploymentEvent.STARTED,
            project,
            run_id,
            commit,
            {"previous_commit": pydash.get(live, "commit"), "domain": domain},
        )
    except BaseException:
        await redis.delete(_get_lock_key(project["id"]))
        raise
    # Intentions: the lock is handed over to the background half, which releases it when it's done -- taken here so
    # a busy project is refused right away (409), and so it covers the commit too.
    spawn(_roll_out(project, conversation_id, run_id, commit, pydash.get(live, "commit"), domain))
    return {"run_id": run_id, "commit": commit}


async def activate_domain(project: Project, conversation_id: int, domain: str) -> UUID | None:
    """Set an attached domain up for the site (certificate, container env, nginx) and only then make it the active
    one (see sites.py). Nothing deployed yet: that's the site's first deployment (deploy, on this domain). Otherwise
    the live commit is released again on this domain, in the background -- no build, not a deployment -- while the
    current domain keeps serving: a switch, a long-running operation whose id is returned, to poll with
    get_domain_switch (None for a first deployment: that's in the deployment log). Either way a user's own domain
    whose records aren't there fails at once (sites.check_domain_ready), and a failure leaves the active domain as it
    was. Raises DeploymentInProgress (a deployment or another setup is running), NothingToDeploy."""
    live = await containers.get_live_release(project["id"])
    if live is None:
        await deploy(project, conversation_id, enforce_cooldown=False, domain=domain)
        return None
    if not await redis.set_if_absent(_get_lock_key(project["id"]), DEPLOYMENT_LOCK_TTL_SECONDS, domain):
        raise DeploymentInProgress(f"project {project['id']} is being deployed")
    try:
        operation_id = await long_running_ops.start(
            {"kind": _DOMAIN_SWITCH, "project_id": project["id"], "domain": domain, "commit": live["commit"]}
        )
    except BaseException:
        await redis.delete(_get_lock_key(project["id"]))
        raise
    # Intentions: the lock is handed over to _switch_domain, which releases it (see deploy).
    spawn(_switch_domain(project, conversation_id, live["commit"], domain, operation_id))
    return operation_id


async def get_domain_switch(project: Project, operation_id: UUID) -> dict | None:
    """How a domain switch (activate_domain's operation) went: {operation_id, domain, state: running | succeeded |
    failed | interrupted, cause, error, started_at, finished_at} -- None if it isn't one of this project's.
    Informational only: nothing decides from it. [I/O: reads]"""
    events = await long_running_ops.get_events(operation_id)
    return _fold_domain_switch(project, operation_id, events, datetime.now(UTC))


def _fold_domain_switch(project: Project, operation_id: UUID, events: list[dict], now: datetime) -> dict | None:
    """[Pure] A domain switch's events (its start, then its outcome) -> get_domain_switch's shape. One that never got
    an outcome within DOMAIN_SWITCH_DEADLINE_SECONDS was interrupted (the backend died mid-way)."""
    started = pydash.head(events)
    if (
        not started
        or started["value"].get("kind") != _DOMAIN_SWITCH
        or started["value"].get("project_id") != project["id"]
    ):
        return None
    outcome = pydash.find(events, lambda event: bool(event["value"].get("outcome")))
    deadline = started["created_at"] + timedelta(seconds=DOMAIN_SWITCH_DEADLINE_SECONDS)
    unfinished_state = "interrupted" if now > deadline else "running"
    return {
        "operation_id": operation_id,
        "domain": started["value"]["domain"],
        "state": outcome["value"]["outcome"] if outcome else unfinished_state,
        "cause": pydash.get(outcome, "value.cause"),
        "error": pydash.get(outcome, "value.error"),
        "started_at": started["created_at"],
        "finished_at": pydash.get(outcome, "created_at"),
    }


async def get_activation(project: Project) -> dict:
    """The domain being set up right now (None if none: the lock's value) and the last first release on a domain that
    failed, while no domain has become active since: {domain, failure: {domain, cause, error, at} | None} -- read
    from the deployment log. `cause`: dns (a user's own domain without its records), certificate (its certificate /
    nginx), release (the site itself didn't come up). A switch's failure isn't here: it's told through its operation
    (get_domain_switch)."""
    lock_value, deployments, site_domains = await asyncio.gather(
        redis.get_value(_get_lock_key(project["id"])), list_deployments(project), sites.get_site_domains(project)
    )
    last_release_on_domain = pydash.find(deployments, lambda run: bool(run["domain"]))
    failure = (
        {**pydash.pick(last_release_on_domain, "domain", "cause", "error"), "at": last_release_on_domain["finished_at"]}
        if last_release_on_domain and last_release_on_domain["outcome"] == "failed"
        else None
    )
    active_since = pydash.get(pydash.find(site_domains, lambda row: row["activated_at"] is not None), "activated_at")
    is_news = failure and not (active_since and active_since > failure["at"])
    return {"domain": lock_value or None, "failure": failure if is_news else None}


async def get_deployment_status(project: Project, conversation_id: int) -> dict:
    """Where the project's site stands, all read live:
    live                    {commit, deployed_at, running} from docker, None if never deployed
    deploying               whether a deployment runs right now
    has_undeployed_changes  whether the workspace differs from the live commit; None when we can't tell
                            (the dev container is stopped -- not worth waking for a status)
    can_deploy              there's an app, nothing's deploying, and there's something new to deploy (or
                            we can't tell)
    active_domain, urls     the domain the site is set up for and served on (sites.py), None before it has one
    site_domains            every domain attached to it: [{domain, kind, activated_at, created_at, servable}]
    activation              get_activation
    cooldown_until          when a user may deploy again (None = now)
    last_deployment         the newest entry of list_deployments, None if never deployed"""
    live = await containers.get_live_release(project["id"])
    deploying = await redis.flag_exists(_get_lock_key(project["id"]))
    has_app = devenv.has_app(project, conversation_id)
    has_changes = await sources.has_changes_since(project, conversation_id, pydash.get(live, "commit"))
    deployments = await list_deployments(project)
    active_domain = await sites.get_active_domain(project)
    site_domains = await sites.get_site_domains(project)
    servable = await asyncio.gather(*[sites.is_servable(project, row) for row in site_domains])
    cooldown_until = await get_cooldown_until(project)
    return {
        "live": live,
        "deploying": deploying,
        "has_undeployed_changes": has_changes,
        "can_deploy": has_app and not deploying and has_changes is not False,
        "active_domain": active_domain,
        "urls": sites.to_site_urls([active_domain] if active_domain else []),
        "site_domains": [
            {**row, "servable": is_servable} for row, is_servable in zip(site_domains, servable, strict=True)
        ],
        "activation": await get_activation(project),
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
    live_commit, attempts: [{attempt, error, at}], error, domain, cause}. outcome is the last event's (succeeded /
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
        # A first release on a domain: which one, and (if it failed) what kind of failure it was (_to_cause).
        "domain": pydash.get(started, "value.domain"),
        "cause": pydash.get(last, "value.cause") if finished else None,
    }


async def ensure_release(project: Project, conversation_id: int, commit: str, domain: str | None) -> None:
    """Make sure `commit` is what the project's site runs, on `domain` (None: on none, so no nginx site) -- every
    step skipped when it's already done (see the top of this file)."""
    image_tag = images.get_image_tag(project["id"], commit)
    if not await images.image_exists(image_tag):
        await devenv.ensure_container(project)  # the archive comes out of it, and an idle sweep may have stopped it
        await images.build_image(
            project["id"], image_tag, await sources.archive_commit(project, conversation_id, commit)
        )
    await environment.ensure_database_access(project)
    domains = [domain] if domain else []
    site_env = environment.render_site_env(project, sites.to_site_urls(domains))
    await containers.ensure_site_container(project, image_tag, commit, site_env)
    await sites.ensure_site(project, domains)


async def _roll_out(
    project: Project,
    conversation_id: int,
    run_id: str,
    commit: str,
    previous_commit: str | None,
    new_domain: str | None,
) -> None:
    """deploy's background half: _release_or_roll_back, then the lock goes."""
    try:
        await _release_or_roll_back(project, conversation_id, run_id, commit, previous_commit, new_domain)
    finally:
        await redis.delete(_get_lock_key(project["id"]))


async def _release_or_roll_back(
    project: Project,
    conversation_id: int,
    run_id: str,
    commit: str,
    previous_commit: str | None,
    new_domain: str | None,
) -> None:
    """Roll `commit` out (with retries) on `new_domain` if given -- active once it's up, and checked first
    (sites.check_domain_ready: no records, no tries) -- else on the active domain; if it never comes up, roll
    `previous_commit` back out on the active domain (with retries too). Every outcome lands in the log, errors in
    the error log too -- this is where they stop."""
    active_domain = await sites.get_active_domain(project)
    try:
        if new_domain:
            await sites.check_domain_ready(project, new_domain)
        await _ensure_release_retrying(project, conversation_id, run_id, commit, new_domain or active_domain)
        if new_domain:
            await site_domains_repo.activate(project["id"], new_domain)
        await _record(DeploymentEvent.SUCCEEDED, project, run_id, commit)
        await images.prune_images(project["id"], images.get_image_tag(project["id"], commit))
        return
    except Exception as error:
        log_error(error, f"deploying project {project['id']} failed", {"run_id": run_id, "commit": commit})
        failure = error
    if not previous_commit or previous_commit == commit:
        await _record(DeploymentEvent.FAILED, project, run_id, commit, _to_failure_value(failure))
        return
    try:
        await _ensure_release_retrying(project, conversation_id, run_id, previous_commit, active_domain)
        await _record(DeploymentEvent.ROLLED_BACK, project, run_id, previous_commit, _to_failure_value(failure))
    except Exception as rollback_error:
        log_error(rollback_error, f"rolling project {project['id']} back failed", {"run_id": run_id})
        await _record(DeploymentEvent.FAILED, project, run_id, previous_commit, _to_failure_value(rollback_error))


async def _ensure_release_retrying(
    project: Project, conversation_id: int, run_id: str | None, commit: str, domain: str | None, attempt: int = 1
) -> None:
    """Recursive: ensure_release, and on a failure log the attempt and go again after a pause -- up to
    DEPLOYMENT_ATTEMPTS, then the last error goes up. `run_id` None: not a deployment (a domain switch), so its
    attempts aren't logged."""
    try:
        await ensure_release(project, conversation_id, commit, domain)
    except Exception as error:
        if run_id:
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
        await _ensure_release_retrying(project, conversation_id, run_id, commit, domain, attempt + 1)


async def _switch_domain(project: Project, conversation_id: int, commit: str, domain: str, operation_id: UUID) -> None:
    """activate_domain's background half, with a live site: release the live commit on `domain` (checked first,
    then with retries, like a deployment), then it's the active one. On a failure, release it again on the domain
    that's still active -- the setup may have got half-way (the container already on the new domain's env). The
    outcome goes to its operation, for the user to see. Then the lock goes."""
    try:
        await sites.check_domain_ready(project, domain)
        await _ensure_release_retrying(project, conversation_id, None, commit, domain)
        await site_domains_repo.activate(project["id"], domain)
        await long_running_ops.record(operation_id, {"outcome": "succeeded"})
    except Exception as error:
        log_error(error, f"setting up {domain} for project {project['id']} failed", {"domain": domain})
        await long_running_ops.record(operation_id, {"outcome": "failed", **_to_failure_value(error)})
        try:
            await ensure_release(project, conversation_id, commit, await sites.get_active_domain(project))
        except Exception as restore_error:
            log_error(restore_error, f"restoring project {project['id']}'s site after a failed setup failed")
    finally:
        await redis.delete(_get_lock_key(project["id"]))


def _to_failure_value(error: Exception) -> dict:
    """[Pure] What a failed release records: {error, cause} (cause: see get_activation)."""
    return {"error": _to_error_text(error), "cause": _to_cause(error)}


def _to_cause(error: Exception) -> str:
    """[Pure]"""
    if isinstance(error, sites.DomainNotReady):
        return "dns"
    return "certificate" if isinstance(error, sites.SiteSetupFailed) else "release"


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
