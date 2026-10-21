# Intentions: the endpoints a conversation's agent SESSION calls back into -- not the browser. The
# session authenticates with its session token (services/agent/tokens), which also says which
# conversation it speaks for, so none of these take an id: a session can only ever touch its own.
#   POST /agent/messages     a message the session produced, for its conversation (422 for content the
#                            agent may not send)
#   POST /agent/signals      {slug, description?, meta?}: a signal from the agent (BUILDING, BUILDING_DONE);
#                            403 for the ones only we write
#   POST /agent/dev-servers  {action}: status / start / restart / stop the project's dev servers
#   /agent/model/{path}      the model API, with OUR key (services/agent2/models): an agent's Claude Code
#                            never holds the key. ONLY on the agent listener (listener_router, see src/app.py),
#                            so a session token copied out of a container can't spend on our account
# The agent listener (src/app.py, on the containers' gateway) serves all of these; the public API serves
# `router` too, for the tmux runtime's sessions on the host.
# Starting implementing and signing it off aren't the agent's to call: the user approves them in the chat
# (services/agent/implementation).

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.dto.ai_conversations import AiMessage, PostAgentMessageRequest, PostAgentSignalRequest, PostDevServersRequest
from src.services import activity, agent2
from src.services import agent as agent_service
from src.utils.logging import log_http_error

_bearer = HTTPBearer(auto_error=False)


async def get_session_conversation_id(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> int:
    """Route dependency: the conversation the caller's session token speaks for. 403 without a valid one.
    Every agent call also counts as the conversation being in use (services/activity): an agent at work keeps
    its project's container up, model calls included."""
    conversation_id = agent_service.get_token_conversation_id(creds.credentials) if creds else None
    if conversation_id is None:
        raise HTTPException(status_code=403, detail="FORBIDDEN")
    await activity.mark_active(conversation_id)
    return conversation_id


router = APIRouter()
# Only on the agent listener (src/app.py), never the public API.
listener_router = APIRouter()


@router.post("/agent/messages", status_code=201)
async def post_session_message(
    body: PostAgentMessageRequest, request: Request, conversation_id: int = Depends(get_session_conversation_id)
) -> AiMessage:
    try:
        return await agent_service.receive_session_message(conversation_id, body.model_dump()["content"])
    except agent_service.ConversationNotFound as error:
        raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND") from error
    except agent_service.InvalidSessionMessage as error:
        raise HTTPException(status_code=422, detail={"code": "INVALID_MESSAGE", "reason": error.args[0]}) from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/agent/signals", status_code=201)
async def post_session_signal(
    body: PostAgentSignalRequest, request: Request, conversation_id: int = Depends(get_session_conversation_id)
) -> AiMessage:
    try:
        return await agent_service.receive_session_signal(conversation_id, body.slug, body.description, body.meta)
    except agent_service.ConversationNotFound as error:
        raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND") from error
    except agent_service.SignalNotAllowed as error:
        raise HTTPException(status_code=403, detail="SIGNAL_NOT_ALLOWED") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/agent/dev-servers")
async def post_dev_servers(
    body: PostDevServersRequest, request: Request, conversation_id: int = Depends(get_session_conversation_id)
) -> dict:
    try:
        return {"servers": await agent_service.control_dev_servers(conversation_id, body.action)}
    except agent_service.ConversationNotFound as error:
        raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND") from error
    except agent_service.ImplementationNotStarted as error:
        raise HTTPException(status_code=409, detail="IMPLEMENTATION_NOT_STARTED") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@listener_router.api_route("/agent/model/{path:path}", methods=["GET", "POST"])
async def proxy_model(path: str, request: Request, conversation_id: int = Depends(get_session_conversation_id)):
    try:
        upstream = await agent2.open_model_request(
            request.method, path, request.url.query, dict(request.headers), await request.body()
        )
    except agent2.ModelPathRefused as error:
        raise HTTPException(status_code=404, detail="NOT_FOUND") from error
    except agent2.ModelUnavailable as error:
        log_http_error(error, request)
        raise HTTPException(status_code=502, detail="MODEL_UNAVAILABLE") from error

    async def _stream():
        try:
            async for chunk in upstream.content.iter_any():
                yield chunk
        finally:
            upstream.release()

    passed_on = {
        name: value
        for name, value in upstream.headers.items()
        if name.lower() in ("content-type", "content-encoding", "request-id")
    }
    return StreamingResponse(_stream(), status_code=upstream.status, headers=passed_on)
