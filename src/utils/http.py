# Intentions: a generic pooled async HTTP client for outbound calls. ONE shared aiohttp ClientSession
# per process (keep-alive connection reuse) -- aiohttp has no module-level request fn and discourages
# a session-per-request, so the pool must be long-lived. Connection-module convention: connect() once
# at startup, close() at shutdown (see utils/postgres for the note). Domain-agnostic: no auth/proxy/
# retry policy here -- callers (e.g. the Amazon client) layer that on top. The httpx-style `request`
# returns PLAIN data (status, body, headers), not a live response, because the body is read here while
# the session context is open.

import json as jsonlib
from collections.abc import Mapping

import aiohttp

_session: aiohttp.ClientSession | None = None


# ci:trivial
async def connect() -> None:
    global _session
    if _session is None:
        _session = aiohttp.ClientSession()


# ci:trivial
def session() -> aiohttp.ClientSession:
    assert _session is not None, "http not connected; call connect() at startup"
    return _session


# ci:trivial
async def request(
    method: str,
    url: str,
    *,
    json: dict | None = None,
    data: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    proxy: str | None = None,
    proxy_auth: aiohttp.BasicAuth | None = None,
    timeout_seconds: float = 30,
) -> tuple[int, dict, Mapping[str, str]]:
    """httpx-style request over the shared pool: `method` is the HTTP verb as a string. Pass `json` for
    a JSON body or `data` for a form-encoded one (x-www-form-urlencoded, e.g. OAuth). Returns
    (status_code, parsed_json_body, headers). The body is read here (the response can't outlive the
    session context), so callers get plain data. A non-JSON body yields {} (e.g. an HTML error page) --
    callers branch on the status, not the body, for failures. `proxy` is used only if given. [I/O]"""
    async with session().request(
        method,
        url,
        json=json,
        data=data,
        headers=dict(headers or {}),
        proxy=proxy,
        proxy_auth=proxy_auth,
        timeout=aiohttp.ClientTimeout(total=timeout_seconds),
    ) as resp:
        text = await resp.text()
        try:
            body = jsonlib.loads(text) if text else {}
        except ValueError:
            body = {}
        return resp.status, body, dict(resp.headers)


# ci:trivial
async def close() -> None:
    global _session
    if _session is not None:
        await _session.close()
        _session = None
