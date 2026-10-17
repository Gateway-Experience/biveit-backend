# Intentions: project business logic that spans more than the projects row -- getting a project ready
# when the user opens it (open_project: its current conversation's workspace, agent session -- started
# off with the starting form's answers the first time -- and, once implementing, dev environment, as
# its stage says), turning it all off again (close_project), deleting it (delete_project), and the
# user's edits from the project list and sidebar (update_project: rename, pin, file into a folder), and
# finding projects by what the user wrote while prompting (search_prompts).
# The workspace is per CONVERSATION (see services/devenv); keeping current_port up to date is
# services/agent's job. Plain CRUD skips this layer and goes straight to repositories/projects.

from typing import Any

import pydash

from src.constants import AGENT_MOCK_REPLIES, PROJECT_SEARCH_EXCERPT_CONTEXT, PROJECT_SEARCH_MIN_LENGTH, ProjectStage
from src.dto.projects import Project
from src.repositories import ai_conversations as ai_conversations_repo
from src.repositories import ai_messages as ai_messages_repo
from src.repositories import project_folders as project_folders_repo
from src.repositories import projects as projects_repo
from src.services import agent as agent_service
from src.services import devenv
from src.utils.common import spawn, utc_now
from src.utils.logging import log_error


def to_excerpt(text: str, query: str) -> str:
    """[Pure] The part of `text` around the first (case-insensitive) occurrence of `query`, on one line,
    with … where it was cut -- enough for the user to see why the project matched."""
    flat = " ".join(text.split())
    start = flat.lower().find(query.lower())
    if start < 0:
        return pydash.truncate(flat, sum(PROJECT_SEARCH_EXCERPT_CONTEXT))
    before, after = PROJECT_SEARCH_EXCERPT_CONTEXT
    window_start = max(0, start - before)
    window_end = min(len(flat), start + len(query) + after)
    # Don't open or close on half a word.
    window_start = (
        flat.find(" ", window_start) + 1 if window_start > 0 and flat[window_start - 1] != " " else window_start
    )
    tail_space = flat.rfind(" ", start + len(query), window_end)
    window_end = tail_space if window_end < len(flat) and tail_space > 0 else window_end
    return f"{'…' if window_start > 0 else ''}{flat[window_start:window_end]}{'…' if window_end < len(flat) else ''}"


async def search_prompts(user_id: int, query: str) -> list[dict]:
    """The user's projects where they wrote `query` while prompting (their starting description or a chat
    message), as [{project_id, excerpt}]. Matching names is the FE's job -- it already has the list. A
    query shorter than PROJECT_SEARCH_MIN_LENGTH finds nothing: one letter would match almost everything."""
    text = " ".join(query.split())
    if len(text) < PROJECT_SEARCH_MIN_LENGTH:
        return []
    matches = await ai_messages_repo.find_prompt_matches(user_id, text)
    return [
        {"project_id": match["project_id"], "excerpt": to_excerpt(match["matched_text"], text)} for match in matches
    ]


class FolderNotFound(Exception):
    """A project was being moved into a folder the project's owner doesn't have. 404."""


async def update_project(project: Project, changes: dict[str, Any]) -> Project | None:
    """Apply the user's edits to their project and return it updated (None if it vanished meanwhile).
    `changes` holds only what the user sent, out of: name, is_pinned, folder_id (None = out of its folder).

    Only a rename counts as editing the project (it bumps updated_at, moving it up the recents); pinning
    and filing just organise the list. Raises FolderNotFound for a folder that isn't the owner's."""
    folder_id = changes.get("folder_id")
    if folder_id is not None and not await project_folders_repo.find({"id": folder_id, "user_id": project["user_id"]}):
        raise FolderNotFound("no such folder", {"folder_id": folder_id, "project_id": project["id"]})
    now = utc_now()
    updates = {
        **({"name": changes["name"], "updated_at": now} if changes.get("name") else {}),
        **({"pinned_at": now if changes["is_pinned"] else None} if changes.get("is_pinned") is not None else {}),
        **({"folder_id": folder_id} if "folder_id" in changes else {}),
    }
    return await projects_repo.update(project["id"], updates) if updates else project


async def open_project(project: Project) -> None:
    """Get a project ready for the user who just opened it, so the agent doesn't have to cold-start on
    their first message. Idempotent -- every visit (or tab) can call it.

    It prepares the project's CURRENT conversation (created on first use). While AGENT_MOCK_REPLIES is
    on there's no real agent to start, so this only seeds its workspace; otherwise it brings up the
    session, and the first time, hands the agent the starting form's answers so it opens the chat
    (ensure_introduced -- once only, and retried by the next open if it failed). It returns once the
    agent has them, so the FE can show the chat right after. It's a warm-up, not a guarantee: a session
    can still die mid-visit, so sending a message to the agent must ensure the session again on its own.

    Once implementing, the dev environment is ensured too -- in the background, since even a quick
    check of it takes seconds. That also finishes a setup that never completed and brings back dev
    servers a reboot (or a crash) took down. Its failures are only logged: the agent finds out on its
    own when it next uses them."""
    conversation = await agent_service.get_current_conversation(project["id"], project["user_id"])
    if AGENT_MOCK_REPLIES:
        await devenv.ensure_workspace(project, conversation["id"])
    else:
        await agent_service.ensure_session(project, conversation["id"])
        await agent_service.ensure_introduced(project, conversation["id"])
    if project["stage"] == ProjectStage.IMPLEMENTING:
        spawn(agent_service.restore_devenv(project, conversation["id"]))


async def close_project(project: Project) -> dict:
    """Turn off everything running for the project: each conversation's agent session (Claude and its
    communication MCP) and the project's dev servers. Nothing on disk is touched, so open_project brings
    it all back. Idempotent: what's already off stays off. Returns what was stopped, as
    {sessions: {conversation_id: adapter.stop_session's result}, dev_servers: [pm2 names removed]}.

    The dev servers belong to the project, but their lock is per conversation -- the current one's is
    used, the same one setting them up takes."""
    conversations = await ai_conversations_repo.find({"project_id": project["id"]})
    sessions = {
        conversation["id"]: await agent_service.stop_session(project, conversation["id"])
        for conversation in conversations
    }
    dev_servers = await agent_service.stop_devenv(project, conversations[0]["id"]) if conversations else []
    return {"sessions": sessions, "dev_servers": dev_servers}


async def delete_project(project: Project) -> None:
    """Delete a project for its owner: turn off whatever runs for it (close_project), then soft-delete the
    row, so it drops out of every read (the repo skips deleted_at rows). Nothing on disk is removed, so a
    project deleted by mistake can still be brought back by hand (clear deleted_at).

    Stopping is best effort: a session or dev server that won't stop must not keep the user from deleting
    the project, so its failure is only logged (whatever's left running finds no live project behind it)."""
    try:
        await close_project(project)
    except Exception as error:
        log_error(error, "delete_project: couldn't stop everything", {"project_id": project["id"]})
    await projects_repo.update(project["id"], {"deleted_at": utc_now()})
