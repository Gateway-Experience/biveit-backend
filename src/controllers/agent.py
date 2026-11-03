# Intentions: the endpoints a conversation's agent SESSION calls back into -- not the browser. The
# session authenticates with its session token (services/agent/tokens), which also says which
# conversation it speaks for, so none of these take an id: a session can only ever touch its own.
#   POST /agent/messages     a message the session produced, for its conversation (422 for content the
#                            agent may not send)
#   POST /agent/signals      {slug, description?, meta?}: a signal from the agent (BUILDING, BUILDING_DONE);
#                            403 for the ones only we write
#   POST /agent/dev-servers  {action}: status / start / restart / stop the project's dev servers
#   POST /agent/launch/plans            {plans_on_sale (the catalog), this_site (what the project has bought)}
#                                       (services/launch/status.get_plans_overview)
#   POST /agent/launch/domains          {query}: domains to buy, with our price (the launch flow's domain search;
#                                       an empty query gives ideas around the project's name). 502 REGISTRAR_ERROR
#   POST /agent/launch/domain-discount  {discount (how it works), this_site ({eligible}: would it get it now)}
#                                       Read-only, all three: the agent can tell the user about plans and domains,
#                                       never buy anything (that's the user's, in the Deploy tab).
#   /agent/model/{path}      the model API, with OUR key (services/agent/model_proxy): an agent's Claude Code
#                            never holds the key. Every answer's usage is metered, and past the hard AI limit
#                            calls are refused (403, in the model API's error shape) -- services/agent/usage
# Only the agent listener (src/app.py, on the containers' gateway) serves these, never the public API -- so
# a session token copied out of a container can't spend on our account from outside.
# Starting the Building stage and signing it off aren't the agent's to call: the user approves them in the chat
# (services/agent/stages).

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.dto.ai_conversations import (
    AiMessage,
    FindDomainsRequest,
    PostAgentMessageRequest,
    PostAgentSignalRequest,
    PostDevServersRequest,
)
from src.modules import domains, payments
from src.repositories import users as users_repo
from src.services import activity
from src.services import agent as agent_service
from src.services.launch import offers, status
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
    except agent_service.BuildingNotStarted as error:
        raise HTTPException(status_code=409, detail="BUILDING_NOT_STARTED") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/agent/launch/plans")
async def post_launch_plans(request: Request, conversation_id: int = Depends(get_session_conversation_id)) -> dict:
    try:
        return await status.get_plans_overview(await agent_service.get_project_from_conv_id(conversation_id))
    except agent_service.ConversationNotFound as error:
        raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/agent/launch/domains")
async def post_launch_domains(
    body: FindDomainsRequest, request: Request, conversation_id: int = Depends(get_session_conversation_id)
) -> dict:
    try:
        project = await agent_service.get_project_from_conv_id(conversation_id)
        # The owner's sandbox mode decides which registrar is asked, the same as their own domain step.
        owner = await users_repo.get(project["user_id"])
        return {"domains": await offers.search_offers(project, body.query, payments.is_sandbox(owner or {}))}
    except agent_service.ConversationNotFound as error:
        raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND") from error
    except domains.InvalidDomainQuery:
        return {"domains": []}
    except domains.RegistrarError as error:
        log_http_error(error, request)
        raise HTTPException(status_code=502, detail="REGISTRAR_ERROR") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/agent/launch/domain-discount")
async def post_launch_domain_discount(
    request: Request, conversation_id: int = Depends(get_session_conversation_id)
) -> dict:
    try:
        return await status.get_domain_discount(await agent_service.get_project_from_conv_id(conversation_id))
    except agent_service.ConversationNotFound as error:
        raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.api_route("/agent/model/{path:path}", methods=["GET", "POST"])
async def proxy_model(path: str, request: Request, conversation_id: int = Depends(get_session_conversation_id)):
    try:
        await agent_service.check_hard_limits(conversation_id)
    except agent_service.AiLimitReached as limit:
        # In the model API's own error shape, so Claude Code reports it. A permission error, not a rate limit:
        # Claude Code retries 429s, and retrying won't help until the day's over.
        message = f"Daily AI limit reached ({limit.scope}); it resets at {limit.resets_at.isoformat()}."
        return JSONResponse(
            status_code=403, content={"type": "error", "error": {"type": "permission_error", "message": message}}
        )
    try:
        upstream = await agent_service.open_model_request(
            request.method, path, request.url.query, dict(request.headers), await request.body()
        )
    except agent_service.ModelPathRefused as error:
        raise HTTPException(status_code=404, detail="NOT_FOUND") from error
    except agent_service.ModelUnavailable as error:
        log_http_error(error, request)
        raise HTTPException(status_code=502, detail="MODEL_UNAVAILABLE") from error

    async def _stream():
        try:
            async for chunk in agent_service.meter_model_stream(conversation_id, upstream.content.iter_any()):
                yield chunk
        finally:
            upstream.release()

    passed_on = {
        name: value
        for name, value in upstream.headers.items()
        if name.lower() in ("content-type", "content-encoding", "request-id")
    }
    return StreamingResponse(_stream(), status_code=upstream.status, headers=passed_on)
