# Intentions: the endpoints a project's agent SESSION calls back into -- not the browser.
#   POST /agent/{port}/messages  (admin token)  a message the session produced, for its conversation
# The session identifies itself by the port we launched it on. The caller is our own process, not a
# user, so it authenticates with the admin token (it inherits ADMIN_TOKEN from the backend's env).

from fastapi import APIRouter, Depends, HTTPException, Request

from src.dto.ai_conversations import AiMessage, PostAgentMessageRequest
from src.services import agent as agent_service
from src.utils.auth import require_superuser
from src.utils.logging import log_http_error

router = APIRouter(dependencies=[Depends(require_superuser)])


@router.post("/agent/{port}/messages", status_code=201)
async def post_session_message(port: int, body: PostAgentMessageRequest, request: Request) -> AiMessage:
    try:
        return await agent_service.receive_session_message(port, body.model_dump()["content"])
    except agent_service.SessionNotFound as error:
        raise HTTPException(status_code=404, detail="SESSION_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
