# Intentions: the browser's side of the agent chat.
#   GET  /projects/{project_id}/conversation          (auth)  the project's current conversation (created on first use)
#   GET  /conversations/{conversation_id}/messages    (auth)  a page of messages (see list_messages)
#   POST /conversations/{conversation_id}/messages    (auth)  send text, or answer/abort a pending interaction
#   GET  /conversations/{conversation_id}/events      (auth)  SSE stream of new messages
#   POST /conversations/{conversation_id}/uploads     (auth)  store attached files (multipart `files`), before sending
#   GET  /conversations/{conversation_id}/files       (auth)  download a file: ?path=uploads/... or artifacts/...
#   GET  /conversations/{conversation_id}/artifacts   (auth)  the artifacts list (the Documents tab)
#   GET  /conversations/{conversation_id}/devenv      (auth)  the dev environment: stage, preview URL, servers
# Every conversation route checks the conversation belongs to one of the caller's projects (404 if not).
#
# SSE PROTOCOL: each message is an `event: message` with `id: <seq>` and the message row as JSON `data`.
# EventSource resends the last id as Last-Event-ID when it reconnects, and the stream resumes right
# after it -- so a dropped connection never loses or repeats a message. A `: heartbeat` comment goes out
# when idle; the stream closes itself every SSE_MAX_AGE_SECONDS and the browser just reconnects.
# An `event: devenv` (data {}, no id) is only a hint that the dev environment changed: re-read GET .../devenv.
# Auth rides the session cookie (EventSource can't send headers), so open it with withCredentials.

import json
import os

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from src.constants import (
    MESSAGES_PAGE_DEFAULT,
    MESSAGES_PAGE_MAX,
    SSE_HEARTBEAT_SECONDS,
    SSE_MAX_AGE_SECONDS,
    SSE_RETRY_MS,
)
from src.dto.ai_conversations import AiConversation, AiMessage, PostUserMessageRequest
from src.dto.users import User
from src.repositories import ai_messages as ai_messages_repo
from src.services import agent as agent_service
from src.utils.auth import get_current_user
from src.utils.logging import log_http_error

router = APIRouter()


@router.get("/projects/{project_id}/conversation")
async def get_current_conversation(
    project_id: int, request: Request, user: User = Depends(get_current_user)
) -> AiConversation:
    try:
        return await agent_service.get_current_conversation(project_id, user["id"])
    except agent_service.ProjectNotFound as error:
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: int,
    request: Request,
    before_seq: int | None = Query(None, ge=1),
    after_seq: int | None = Query(None, ge=0),
    limit: int = Query(MESSAGES_PAGE_DEFAULT, ge=1, le=MESSAGES_PAGE_MAX),
    user: User = Depends(get_current_user),
) -> list[AiMessage]:
    """A page of messages as a bare array, always in seq order:
      - no cursor        -> the newest `limit` (opening the chat)
      - before_seq=S     -> the `limit` before S (scrolling up; S = the oldest seq the client has)
      - after_seq=N      -> the `limit` after N (catching up; the events stream normally does this)
    Backward pages never start on an answer -- they reach back to its question, so they can run a little
    over `limit`. There's no has_more: seq starts at 1 with no gaps, so more history exists exactly when
    the first message's seq is > 1."""
    if before_seq is not None and after_seq is not None:
        raise HTTPException(status_code=422, detail="USE_ONE_CURSOR")
    try:
        conversation = await agent_service.get_owned_conversation(conversation_id, user["id"])
        if after_seq is not None:
            return await ai_messages_repo.find(conversation["id"], after_seq=after_seq, limit=limit)
        return await ai_messages_repo.find_before(conversation["id"], before_seq, limit)
    except agent_service.ConversationNotFound as error:
        raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/conversations/{conversation_id}/messages", status_code=201)
async def post_message(
    conversation_id: int, body: PostUserMessageRequest, request: Request, user: User = Depends(get_current_user)
) -> AiMessage:
    """Returns the user's own stored message. The agent's reply comes through the events stream."""
    try:
        conversation = await agent_service.get_owned_conversation(conversation_id, user["id"])
        return await agent_service.post_user_message(conversation, body.model_dump())
    except agent_service.ConversationNotFound as error:
        raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND") from error
    except agent_service.InteractionPending as error:
        raise HTTPException(status_code=409, detail="INTERACTION_PENDING") from error
    except agent_service.InteractionNotFound as error:
        raise HTTPException(status_code=409, detail="INTERACTION_NOT_PENDING") from error
    except agent_service.InvalidSubmission as error:
        raise HTTPException(status_code=422, detail={"code": "INVALID_SUBMISSION", "reason": str(error)}) from error
    except agent_service.FileNotAvailable as error:
        raise HTTPException(status_code=422, detail={"code": "INVALID_ATTACHMENT", "path": str(error)}) from error
    except agent_service.SessionUnavailable as error:
        # The message IS stored; only its delivery to the agent failed.
        log_http_error(error, request)
        raise HTTPException(status_code=503, detail="AGENT_UNAVAILABLE") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/conversations/{conversation_id}/resend")
async def resend_message(conversation_id: int, request: Request, user: User = Depends(get_current_user)) -> AiMessage:
    """Hands the user's latest message to the agent again, after posting it came back 503
    (AGENT_UNAVAILABLE: stored, but it didn't get through). Returns that message; nothing new is stored.
    409 NOTHING_TO_RESEND when the latest message isn't the user's."""
    try:
        conversation = await agent_service.get_owned_conversation(conversation_id, user["id"])
        return await agent_service.resend_last_user_message(conversation)
    except agent_service.ConversationNotFound as error:
        raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND") from error
    except agent_service.NothingToResend as error:
        raise HTTPException(status_code=409, detail="NOTHING_TO_RESEND") from error
    except agent_service.SessionUnavailable as error:
        log_http_error(error, request)
        raise HTTPException(status_code=503, detail="AGENT_UNAVAILABLE") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.get("/conversations/{conversation_id}/events")
async def stream_events(
    conversation_id: int,
    request: Request,
    after_seq: int = Query(0, ge=0),
    last_event_id: int | None = Header(None, alias="Last-Event-ID"),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """SSE stream of the conversation's messages after `after_seq` (or after Last-Event-ID on a
    reconnect, whichever is later). Ownership is checked before the stream opens, so a wrong id is a
    plain 404 rather than a stream that dies."""
    try:
        conversation = await agent_service.get_owned_conversation(conversation_id, user["id"])
    except agent_service.ConversationNotFound as error:
        raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error

    async def _generate_events():
        yield f"retry: {SSE_RETRY_MS}\n\n"
        try:
            async for new_messages, devenv_changed in agent_service.watch_messages(
                conversation["id"], max(after_seq, last_event_id or 0), SSE_HEARTBEAT_SECONDS, SSE_MAX_AGE_SECONDS
            ):
                events = [
                    *[_to_sse_event(message) for message in new_messages],
                    *(["event: devenv\ndata: {}\n\n"] if devenv_changed else []),
                ]
                yield "".join(events) or ": heartbeat\n\n"
        except Exception as error:
            # The 200 is already sent, so all we can do is log and end the stream; EventSource reconnects.
            log_http_error(error, request)

    return StreamingResponse(
        _generate_events(),
        media_type="text/event-stream",
        # X-Accel-Buffering: no -> nginx passes events through as they're written instead of buffering.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/conversations/{conversation_id}/uploads", status_code=201)
async def upload_files(
    conversation_id: int,
    request: Request,
    files: list[UploadFile] = File(...),
    user: User = Depends(get_current_user),
) -> list[dict]:
    """Store files the user is attaching and return them as [{path, name, mime, size}]. Nothing shows
    in the chat until a message is sent with these paths as its `attachments`."""
    try:
        conversation = await agent_service.get_owned_conversation(conversation_id, user["id"])
        return await agent_service.upload_files(
            conversation,
            [{"name": file.filename or "file", "size": file.size or 0, "source": file.file} for file in files],
        )
    except agent_service.ConversationNotFound as error:
        raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND") from error
    except agent_service.UploadRejected as error:
        raise HTTPException(status_code=413, detail={"code": "UPLOAD_REJECTED", "reason": str(error)}) from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.get("/conversations/{conversation_id}/artifacts")
async def list_artifacts(conversation_id: int, request: Request, user: User = Depends(get_current_user)) -> list[dict]:
    """Bare array of {path, name, mime, size, version, title}. Open one through the files endpoint by
    its path; `version` changes whenever the file does."""
    try:
        conversation = await agent_service.get_owned_conversation(conversation_id, user["id"])
        return await agent_service.list_artifacts(conversation)
    except agent_service.ConversationNotFound as error:
        raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.get("/conversations/{conversation_id}/devenv")
async def get_devenv(conversation_id: int, request: Request, user: User = Depends(get_current_user)) -> dict:
    try:
        conversation = await agent_service.get_owned_conversation(conversation_id, user["id"])
        return await agent_service.get_devenv_status(conversation)
    except agent_service.ConversationNotFound as error:
        raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.get("/conversations/{conversation_id}/files")
async def download_file(
    conversation_id: int, request: Request, path: str = Query(...), user: User = Depends(get_current_user)
) -> Response:
    """One file from the conversation's uploads/ or artifacts/ folder, by its relative path.

    CACHING differs by folder. Uploads never change, so they're cacheable for good. Artifacts are
    rewritten in place, so they're `no-cache`: the browser must check back every time, and gets a cheap
    304 when its copy's ETag (the file's version) still matches. Without an explicit Cache-Control,
    browsers guess a freshness window from Last-Modified and could show a stale artifact for a while.
    `private` everywhere: these are per-user files, never for a shared cache.

    Served with `Content-Security-Policy: sandbox` and `nosniff`: these are user-supplied files coming
    from the API's own origin, so an uploaded .html or .svg must never get to run script there (it'd
    run with the user's session cookie)."""
    try:
        conversation = await agent_service.get_owned_conversation(conversation_id, user["id"])
        file_fd, file_info = await agent_service.get_conversation_file(conversation, path)
    except (agent_service.ConversationNotFound, agent_service.FileNotAvailable) as error:
        raise HTTPException(status_code=404, detail="FILE_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
    etag = f'"{file_info["version"]}"'
    headers = {
        "ETag": etag,
        "Cache-Control": "private, max-age=31536000, immutable" if file_info["immutable"] else "private, no-cache",
        "Content-Security-Policy": "sandbox",
        "X-Content-Type-Options": "nosniff",
    }
    if etag in [tag.strip() for tag in request.headers.get("if-none-match", "").split(",")]:
        os.close(file_fd)
        return Response(status_code=304, headers=headers)
    # Served through /proc/self/fd: FileResponse wants a path (it does the ranges video seeking needs), and
    # that one reopens the very file we checked -- never what's at the file's path by now, which the agent
    # could have swapped for a symlink. Linux-only (macOS has no /proc). The file object holds the
    # descriptor: closed after the response, or when it's dropped if the client went away mid-way.
    held_file = os.fdopen(file_fd, "rb")
    response = FileResponse(
        f"/proc/self/fd/{file_fd}",
        media_type=file_info["mime"],
        filename=file_info["name"],
        content_disposition_type="inline",
        headers=headers,
        stat_result=os.fstat(file_fd),
        background=BackgroundTask(held_file.close),
    )
    response.held_file = held_file
    return response


def _to_sse_event(message: AiMessage) -> str:
    """[Pure] One stored message as an SSE event. JSON has no raw newlines, so `data` fits on one line."""
    return f"id: {message['seq']}\nevent: message\ndata: {json.dumps(jsonable_encoder(message))}\n\n"
