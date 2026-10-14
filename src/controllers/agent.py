# Intentions: the endpoints a conversation's agent SESSION calls back into -- not the browser. The
# session authenticates with its session token (services/agent/tokens), which also says which
# conversation it speaks for, so none of these take an id: a session can only ever touch its own.
#   POST /agent/messages     a message the session produced, for its conversation
#   POST /agent/devenv       the agent is ready to implement: set up the dev environment (202; the
#                            outcome is pushed into the session over its channel when it's done)
#   POST /agent/dev-servers  {action}: status / start / restart / stop the project's dev servers
#   POST /agent/building     {active, summary?}: a big change starts (or is refreshed) / ends
#   POST /agent/implementing-done  the user is happy with the implemented site (FE: offer payment/deploy)

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.dto.ai_conversations import AiMessage, PostAgentMessageRequest, PostBuildingRequest, PostDevServersRequest
from src.services import agent as agent_service
from src.utils.logging import log_http_error

_bearer = HTTPBearer(auto_error=False)


def get_session_conversation_id(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> int:
    """Route dependency: the conversation the caller's session token speaks for. 403 without a valid one."""
    conversation_id = agent_service.get_token_conversation_id(creds.credentials) if creds else None
    if conversation_id is None:
        raise HTTPException(status_code=403, detail="FORBIDDEN")
    return conversation_id


router = APIRouter()


@router.post("/agent/messages", status_code=201)
async def post_session_message(
    body: PostAgentMessageRequest, request: Request, conversation_id: int = Depends(get_session_conversation_id)
) -> AiMessage:
    try:
        return await agent_service.receive_session_message(conversation_id, body.model_dump()["content"])
    except agent_service.ConversationNotFound as error:
        raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/agent/devenv", status_code=202)
async def post_devenv(request: Request, conversation_id: int = Depends(get_session_conversation_id)) -> dict:
    try:
        await agent_service.start_implementation(conversation_id)
    except agent_service.ConversationNotFound as error:
        raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
    return {"status": "started"}


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


@router.post("/agent/building")
async def post_building(
    body: PostBuildingRequest, request: Request, conversation_id: int = Depends(get_session_conversation_id)
) -> dict:
    try:
        await agent_service.set_building_status(conversation_id, body.active, body.summary)
    except agent_service.ConversationNotFound as error:
        raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
    return {"building": body.active}


@router.post("/agent/implementing-done")
async def post_implementing_done(request: Request, conversation_id: int = Depends(get_session_conversation_id)) -> dict:
    try:
        return await agent_service.mark_implementing_done(conversation_id)
    except agent_service.ConversationNotFound as error:
        raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND") from error
    except agent_service.ImplementationNotStarted as error:
        raise HTTPException(status_code=409, detail="IMPLEMENTATION_NOT_STARTED") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
