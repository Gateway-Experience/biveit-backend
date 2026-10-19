# Intentions: the chat between a user and their project's agent. Messages come in from two sides --
# the agent's session (receive_session_message) and the user (post_user_message) --
# and go out to the user's open tabs (watch_messages, behind the SSE endpoint).
#
# THE DB LOG IS THE ONLY SOURCE OF TRUTH. Every new message also publishes a tiny wake-up on redis, but
# it carries no data: a watcher that hears it just re-reads the log from its last seq. So a lost
# wake-up (redis hiccup, reconnect) only delays a message until the watcher's next periodic check --
# it can never drop or duplicate one.

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pydash

from src.constants import (
    AGENT_MOCK_REPLIES,
    AGENT_SIGNALS,
    AI_CONVERSATION_LOCK_TTL_SECONDS,
    AI_CONVERSATION_LOCK_WAIT_SECONDS,
    DEVENV_WAKEUP,
    SHOW_ARTIFACT_TOOL_NAME,
    SIGNAL_TAIL_SIZE,
    UPLOAD_MAX_BYTES,
    UPLOAD_MAX_FILES,
    AiMessageRole,
    AiSignal,
)
from src.dto.ai_conversations import AiConversation, AiMessage
from src.dto.projects import Project
from src.repositories import ai_conversations as ai_conversations_repo
from src.repositories import ai_messages as ai_messages_repo
from src.repositories import projects as projects_repo
from src.services import devenv
from src.services.agent import implementation, mock, sessions, signals, storage, wakeups
from src.services.agent.interactions import (
    InvalidSubmission,
    find_pending_interactions,
    get_approved_actions,
    to_tool_result_block,
    validate_interaction_results,
    validate_session_content,
)
from src.utils import redis
from src.utils.common import spawn


class ProjectNotFound(Exception):
    """No such live project, or it isn't the caller's. 404 either way -- don't reveal which."""


class ConversationNotFound(Exception):
    """No such live conversation, or it isn't the caller's. 404 either way."""


class InteractionPending(Exception):
    """The user sent text while the agent is waiting on an answer. They have to answer (or abort) the
    pending interaction first -- the chat input is replaced by the question until then. 409."""


class UploadRejected(Exception):
    """Too many files in one upload, or a file over the size limit. The message says which. 413."""


class InteractionNotFound(Exception):
    """The submission names an interaction that isn't pending (wrong id, or already answered). 409."""


class NothingToResend(Exception):
    """A resend was asked for, but the conversation's latest message isn't the user's (the agent has
    already replied, or nothing's been said yet). 409."""


class SignalNotAllowed(Exception):
    """The agent sent a signal only we may write (see AGENT_SIGNALS). 403."""


# --- Reading ---------------------------------------------------------------------------------------


async def get_current_conversation(project_id: int, user_id: int) -> AiConversation:
    """The caller's project's current conversation, created on first use."""
    projects = await projects_repo.find({"id": project_id, "user_id": user_id})
    if not projects:
        raise ProjectNotFound(f"project {project_id}", {"project_id": project_id})
    return await _ensure_conversation(projects[0])


async def get_owned_conversation(conversation_id: int, user_id: int) -> AiConversation:
    """A conversation, if it belongs to one of the caller's projects."""
    conversation = await ai_conversations_repo.get(conversation_id)
    if conversation is None or not await projects_repo.find({"id": conversation["project_id"], "user_id": user_id}):
        raise ConversationNotFound(f"conversation {conversation_id}", {"conversation_id": conversation_id})
    return conversation


async def get_live_conversation(conversation_id: int) -> AiConversation:
    """A conversation that still exists, whoever it belongs to -- for callers already proven to speak for
    it (its session's token). Raises ConversationNotFound."""
    conversation = await ai_conversations_repo.get(conversation_id)
    if conversation is None:
        raise ConversationNotFound(f"conversation {conversation_id}", {"conversation_id": conversation_id})
    return conversation


async def get_conversation_project(conversation: AiConversation) -> Project:
    """The project a conversation belongs to -- its storage paths hang off the project (see storage)."""
    projects = await projects_repo.find({"id": conversation["project_id"]})
    return projects[0]


async def get_conversation_file(conversation: AiConversation, relative_path: str) -> tuple[Path, dict]:
    """A file from the conversation's uploads/ or artifacts/ folder by its relative path, as (absolute
    path, {path, name, mime, size, version, immutable}). Raises storage.FileNotAvailable for anything
    else -- a missing file, another folder, or a path that resolves outside the conversation.

    `immutable`: uploads never change (each gets a unique name), so they can be cached for good;
    artifacts are rewritten in place, so a cached copy must be revalidated against `version`."""
    project = await get_conversation_project(conversation)
    absolute_path = storage.resolve_conversation_file(project, conversation["id"], relative_path)
    file_info = {
        **storage.describe_file(relative_path, absolute_path),
        "version": storage.get_file_version(absolute_path),
        "immutable": relative_path.startswith(f"{storage.UPLOADS_DIR}/"),
    }
    return absolute_path, file_info


async def list_artifacts(conversation: AiConversation) -> list[dict]:
    """The conversation's artifacts -- the Documents tab -- as [{path, name, mime, size, version, title}].
    `version` changes whenever the file does, so clients can tell which artifacts to refetch.

    The folder is the list: whatever is in artifacts/ is an artifact. `title` comes from the latest
    show_artifact call that showed the file (an artifact can be re-shown with a new title as it evolves),
    falling back to the file name for files the agent wrote but never showed."""
    project = await get_conversation_project(conversation)
    files = await storage.list_files(project, conversation["id"], storage.ARTIFACTS_DIR)
    # Later calls overwrite earlier ones in the dict, so each path keeps its most recent title.
    titles_by_path = {
        block["input"].get("path"): block["input"].get("title")
        for message in await ai_messages_repo.find(conversation["id"])
        for block in message["content"]
        if block.get("type") == "tool_use" and block.get("name") == SHOW_ARTIFACT_TOOL_NAME
    }
    conversation_dir = Path(devenv.get_conversation_dir(project, conversation["id"]))
    return [
        {
            **file,
            "version": storage.get_file_version(conversation_dir / file["path"]),
            "title": titles_by_path.get(file["path"]) or file["name"],
        }
        for file in files
    ]


async def watch_messages(
    conversation_id: int, after_seq: int, idle_seconds: float, max_age_seconds: float
) -> AsyncIterator[tuple[list[AiMessage], bool]]:
    """Yield (new messages, devenv_changed) as things happen: the conversation's new messages in
    batches, starting after `after_seq`, and whether its dev environment changed since the last yield
    (wakeups.publish_devenv_changed) -- a hint to re-read its status, which isn't in the log.

    Yields an EMPTY batch (and False) when `idle_seconds` pass with nothing new -- the caller's cue for a
    heartbeat.
    Each wake-up or idle tick re-reads the log from the last seq sent, so this never misses or repeats a
    message, whatever happens to the wake-ups. Ends after `max_age_seconds` (the client reconnects).

    Subscribes BEFORE the first read: a message landing between the two then still wakes us."""
    deadline = time.monotonic() + max_age_seconds
    async with redis.subscription(wakeups.get_wakeup_channel(conversation_id)) as receive_wakeup:
        last_seq = after_seq
        devenv_changed = False
        # An endless stream by nature, so a loop rather than anything declarative. last_seq is the only
        # real state: the watermark of what this client has. devenv_changed just carries the last
        # wake-up's meaning into the next yield.
        while time.monotonic() < deadline:
            new_messages = await ai_messages_repo.find(conversation_id, after_seq=last_seq)
            yield new_messages, devenv_changed
            last_seq = new_messages[-1]["seq"] if new_messages else last_seq
            devenv_changed = await receive_wakeup(idle_seconds) == DEVENV_WAKEUP


# --- Writing ---------------------------------------------------------------------------------------


async def receive_session_message(conversation_id: int, content: list[dict[str, Any]]) -> AiMessage:
    """Append a message from the conversation's session, and return the stored message. Everything a
    session posts is the agent talking, so it's an ASSISTANT message; its tool_use items get the id and
    name the channel leaves out (sessions.to_stored_content). Raises ConversationNotFound if the
    conversation is gone (deleted while its session ran on), InvalidSessionMessage for content the agent
    may not send (interactions.validate_session_content)."""
    conversation = await get_live_conversation(conversation_id)
    validate_session_content(content, (await get_conversation_project(conversation))["stage"])
    return await _append(conversation["id"], AiMessageRole.ASSISTANT, sessions.to_stored_content(content), {})


async def receive_session_signal(
    conversation_id: int, slug: AiSignal, description: str | None, meta: dict[str, Any] | None
) -> AiMessage:
    """Record a signal the conversation's session sent (its send_signal tool) and return it. Raises
    ConversationNotFound, or SignalNotAllowed for one of the signals only we write."""
    conversation = await get_live_conversation(conversation_id)
    if slug not in AGENT_SIGNALS:
        raise SignalNotAllowed(f"{slug} isn't the agent's to send", {"slug": slug, "allowed": AGENT_SIGNALS})
    return await signals.append_signal(conversation["id"], slug, description, meta)


async def upload_files(conversation: AiConversation, files: list[dict]) -> list[dict]:
    """Store files the user is attaching -- each {name, size, source} -- in the conversation's uploads
    folder, and return them described as {path, name, mime, size}. Nothing is posted to the chat yet:
    the client sends a message listing these paths when the user hits send.

    Limits are checked for the whole batch BEFORE anything is written, so a rejected upload leaves no
    partial files behind."""
    if len(files) > UPLOAD_MAX_FILES:
        raise UploadRejected(f"at most {UPLOAD_MAX_FILES} files per upload, got {len(files)}")
    oversized = [file["name"] for file in files if file["size"] > UPLOAD_MAX_BYTES]
    if oversized:
        raise UploadRejected(f"over {UPLOAD_MAX_BYTES // (1024 * 1024)} MB: {oversized}")

    project = await get_conversation_project(conversation)
    saved_paths = [
        await storage.save_upload(project, conversation["id"], file["name"], file["source"]) for file in files
    ]
    return [_describe_attachment(project, conversation["id"], path) for path in saved_paths]


def _describe_attachment(project: Project, conversation_id: int, relative_path: str) -> dict:
    """[Pure-ish] An uploaded file as {path, name, mime, size}. Raises storage.FileNotAvailable unless
    the path is an existing file in THIS conversation's uploads folder."""
    absolute_path = storage.resolve_conversation_file(project, conversation_id, relative_path, [storage.UPLOADS_DIR])
    return storage.describe_file(relative_path, absolute_path)


async def post_user_message(conversation: AiConversation, body: dict[str, Any]) -> AiMessage:
    """Take a message from the user -- {"type": "text", text, attachments} or {"type": "submission",
    interaction_id, results} -- check it, store it, and return it. The reply arrives separately, through
    watch_messages.

    Attachments are paths from upload_files; each becomes an `attachment` block ({path, name, mime, size},
    re-described from disk rather than trusting the client), placed before the text.

    A submission must answer EVERY pending interaction, each exactly once (409 for an id that isn't
    pending, InvalidSubmission for a missing or repeated one), with results that fit. It's stored as one
    message holding a tool_result per interaction, in the order they were asked. Text is refused while an
    interaction is pending.

    Approvals take effect once the answer is stored and BEFORE it's handed to the agent
    (implementation.apply_approvals): the user's click is what counts, so it holds even if the session
    can't be reached right then (the 503 still comes, and resending only re-delivers)."""
    pending = find_pending_interactions(await ai_messages_repo.find(conversation["id"]))

    if body["type"] == "text":
        if pending:
            raise InteractionPending(f"{len(pending)} pending", {"interaction_ids": pydash.map_(pending, "id")})
        project = await get_conversation_project(conversation)
        attachment_blocks = [
            {"type": "attachment", **_describe_attachment(project, conversation["id"], path)}
            for path in body["attachments"]
        ]
        text_blocks = [{"type": "text", "text": body["text"]}] if body["text"].strip() else []
        return await _append_user(conversation, [*attachment_blocks, *text_blocks])

    results_by_id = {answer["interaction_id"]: answer["results"] for answer in body["answers"]}
    unknown_ids = [
        interaction_id for interaction_id in results_by_id if not pydash.find(pending, {"id": interaction_id})
    ]
    if unknown_ids:
        raise InteractionNotFound(f"not pending: {unknown_ids}", {"interaction_ids": unknown_ids})
    unanswered_ids = [tool_use["id"] for tool_use in pending if tool_use["id"] not in results_by_id]
    if unanswered_ids or len(results_by_id) != len(body["answers"]):
        raise InvalidSubmission(f"answer every pending interaction exactly once (missing: {unanswered_ids})")
    answered = [(tool_use, results_by_id[tool_use["id"]]) for tool_use in pending]
    for tool_use, results in answered:
        validate_interaction_results(tool_use, results)
    # One tool_result per pending tool_use, in the order the agent asked them -- a complete answer to its
    # message, which is how both the session's channel and the LLM API want it.
    user_message = await _append(
        conversation["id"],
        AiMessageRole.USER,
        [to_tool_result_block(tool_use["id"], results) for tool_use, results in answered],
    )
    approved_actions = get_approved_actions(answered)
    if approved_actions:
        project = await get_conversation_project(conversation)
        await implementation.apply_approvals(project, conversation["id"], approved_actions)
    await _deliver(conversation, user_message)
    return user_message


async def resend_last_user_message(conversation: AiConversation) -> AiMessage:
    """Hand the conversation's latest message to the agent again, if it's the user's, and return it.
    It's for a message that was stored but never got through (post_user_message's 503: the session was
    down). Nothing new is stored, so the chat doesn't get a duplicate -- the user just presses "try
    again" instead of retyping. Raises NothingToResend, or SessionUnavailable if it still can't get through.

    "Latest" skips signals: an approval writes some right after the user's answer, and those aren't the
    agent replying.

    Original purpose: the FE's "try again" banner after a failed delivery."""
    tail = await ai_messages_repo.find_before(conversation["id"], None, SIGNAL_TAIL_SIZE)
    latest = pydash.find_last(tail, lambda message: not signals.is_signal_message(message))
    if not latest or latest["role"] != AiMessageRole.USER:
        raise NothingToResend("the latest message isn't the user's", {"conversation_id": conversation["id"]})
    await _deliver(conversation, latest)
    return latest


async def _append_user(conversation: AiConversation, content: list[dict]) -> AiMessage:
    """Store an (already checked) user message, then hand it to whoever answers (_deliver)."""
    user_message = await _append(conversation["id"], AiMessageRole.USER, content)
    await _deliver(conversation, user_message)
    return user_message


async def _deliver(conversation: AiConversation, user_message: AiMessage) -> None:
    """Hand a stored user message to whoever answers: the mock, while AGENT_MOCK_REPLIES is on, or the
    project's real agent session. Raises sessions.SessionUnavailable if the session can't be reached."""
    if not AGENT_MOCK_REPLIES:
        await _send_to_agent(conversation, user_message)
        return
    reply = await _get_mock_reply(conversation, user_message)
    if reply:
        spawn(_append_mock_reply(conversation["id"], reply))


async def _send_to_agent(conversation: AiConversation, user_message: AiMessage) -> None:
    """Forward a just-stored user message to the conversation's agent session, starting the session if it
    isn't running. The message is complete on its own (answers come as a whole batch), so there's nothing
    to look up. The session's reply comes back on its own, through POST /agent/{port}/messages ->
    receive_session_message.

    Raises sessions.SessionUnavailable if the session can't be reached. The message stays stored either
    way -- it just didn't get through."""
    port = await sessions.ensure_session(await get_conversation_project(conversation), conversation["id"])
    await sessions.send(conversation["id"], port, sessions.to_session_content(user_message))


async def _get_mock_reply(conversation: AiConversation, user_message: AiMessage) -> list[dict] | None:
    """The mock's reply to a stored user message, worked out from the message itself: answers are echoed
    (each matched to its question by tool_use_id), attachments get the uploads folder listed, the artifact
    commands write files and show them, and any other text is echoed. None means no reply (an abort).

    The artifact commands need their files on disk BEFORE the reply shows them, so that writing happens
    here rather than in the (pure) mock module."""
    blocks_by_type = pydash.group_by(user_message["content"], "type")
    if "tool_result" in blocks_by_type:
        history = await ai_messages_repo.find(conversation["id"])
        tool_uses_by_id = {
            block["id"]: block for message in history for block in message["content"] if block.get("type") == "tool_use"
        }
        return mock.reply_to_submission(
            [
                (tool_uses_by_id[answered["tool_use_id"]], answered["content"])
                for answered in blocks_by_type["tool_result"]
            ]
        )

    project = await get_conversation_project(conversation)
    if "attachment" in blocks_by_type:
        return mock.reply_to_uploads(await storage.list_files(project, conversation["id"], storage.UPLOADS_DIR))

    text = " ".join(block["text"] for block in blocks_by_type.get("text", []))
    artifact_samples = mock.get_sample_artifacts(text)
    if artifact_samples:
        shown = [
            {
                "path": await storage.write_artifact(project, conversation["id"], sample["name"], sample["content"]),
                "title": sample["title"],
            }
            for sample in artifact_samples
        ]
        return mock.reply_with_artifacts(shown)
    return mock.reply_to_text(text)


async def _append_mock_reply(conversation_id: int, reply: list[dict]) -> None:
    """Store the mock's reply after a pause, in the background, so the FE sees what a real agent looks
    like: the POST returns at once and the answer shows up on the stream a moment later."""
    await asyncio.sleep(mock.REPLY_DELAY_SECONDS)
    await _append(conversation_id, AiMessageRole.ASSISTANT, reply, {"mock": True})


async def _append(conversation_id: int, role: str, content: list[dict], meta: dict | None = None) -> AiMessage:
    """Store a message as the conversation's next one, then wake up anyone watching it. The wake-up is
    best-effort: if redis is down the message is still stored, and watchers pick it up on their next
    periodic check -- so its failure is logged here and swallowed, not bubbled."""
    message = await ai_messages_repo.insert(
        {"conversation_id": conversation_id, "role": role, "content": content, "meta": meta or {}}
    )
    await wakeups.publish_wakeup(conversation_id, str(message["seq"]))
    return message


async def _ensure_conversation(project: Project) -> AiConversation:
    """The project's current conversation -- its newest live one -- created on first use. Locked per
    project: without it, messages arriving together on a fresh project each create their own."""
    async with redis.lock(
        f"ai_conversation:{project['id']}", AI_CONVERSATION_LOCK_TTL_SECONDS, AI_CONVERSATION_LOCK_WAIT_SECONDS
    ):
        conversations = await ai_conversations_repo.find({"project_id": project["id"]})
        if conversations:
            return conversations[0]
        return await ai_conversations_repo.insert({"project_id": project["id"], "user_id": project["user_id"]})
