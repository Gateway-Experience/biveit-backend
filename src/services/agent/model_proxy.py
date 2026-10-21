# Intentions: the model proxy. An agent's Claude Code runs in its project's container, where anything it
# can read the agent can read too -- so it never gets the model key. Its "key" is its session token, its
# model endpoint the backend's agent listener (POST /api/agent/model/v1/messages); we check the token
# (the controller), swap in the real key and pass the call on, streaming the answer straight back. One place
# every model call goes through is also where metering and limits would go.
#
# Only the paths Claude Code needs to talk to the model are forwarded (MODEL_PROXY_PATHS): with our
# key, a provider's other endpoints (files, batches, admin) are ours, not the agent's.

import aiohttp

from src.config import settings
from src.constants import MODEL_PROXY_DEFAULT_BASE_URL, MODEL_PROXY_PATHS, MODEL_PROXY_READ_TIMEOUT_SECONDS
from src.utils import http

# Not passed on: the caller's own credentials (replaced by ours), hop-by-hop ones, and accept-encoding --
# the answer streams back as is, so the endpoint mustn't compress it for us.
_DROPPED_HEADERS = {"authorization", "x-api-key", "host", "content-length", "connection", "accept-encoding"}


class ModelUnavailable(Exception):
    """No model key is configured, or the model endpoint can't be reached. 502."""


class ModelPathRefused(Exception):
    """A path the proxy doesn't forward. 404."""


async def open_model_request(method: str, path: str, query: str, headers: dict, body: bytes) -> aiohttp.ClientResponse:
    """Send an agent's model call on to the model endpoint with our key, and return the open response for
    the caller to stream back -- and release. `path` is what came after /agent/model/ (e.g. v1/messages).
    Raises ModelPathRefused, ModelUnavailable."""
    if path not in MODEL_PROXY_PATHS:
        raise ModelPathRefused(path)
    if not settings.anthropic_api_key and not settings.anthropic_auth_token:
        raise ModelUnavailable("neither ANTHROPIC_API_KEY nor ANTHROPIC_AUTH_TOKEN is set")
    base_url = (settings.anthropic_base_url or MODEL_PROXY_DEFAULT_BASE_URL).rstrip("/")
    forwarded = {name: value for name, value in headers.items() if name.lower() not in _DROPPED_HEADERS}
    try:
        return await http.session().request(
            method,
            f"{base_url}/{path}" + (f"?{query}" if query else ""),
            data=body,
            headers={**forwarded, **_to_auth_headers()},
            auto_decompress=False,
            timeout=aiohttp.ClientTimeout(total=None, sock_read=MODEL_PROXY_READ_TIMEOUT_SECONDS),
        )
    except (OSError, aiohttp.ClientError) as error:
        raise ModelUnavailable(f"the model endpoint is unreachable: {error}") from error


def _to_auth_headers() -> dict[str, str]:
    """[Pure-ish] Our credentials for the model endpoint: another endpoint's token, or Anthropic's API key."""
    if settings.anthropic_auth_token:
        return {"Authorization": f"Bearer {settings.anthropic_auth_token}"}
    return {"x-api-key": settings.anthropic_api_key}
