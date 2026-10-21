# The model proxy (services/agent/model_proxy): what it forwards, with which key -- and that the agent routes,
# the proxy among them, are only on the agent listener, never the public API.

import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.config import settings
from src.controllers.agent import router as agent_router
from src.services.agent import model_proxy


async def test_the_model_proxy_forwards_only_model_calls_with_our_key(monkeypatch):
    sent = {}

    class FakeSession:
        async def request(self, method, url, **kwargs):
            sent.update(method=method, url=url, **kwargs)
            return "response"

    monkeypatch.setattr(settings, "anthropic_base_url", "https://api.deepseek.com/anthropic")
    monkeypatch.setattr(settings, "anthropic_auth_token", "sk-ds-real")
    monkeypatch.setattr(model_proxy.http, "session", lambda: FakeSession())
    headers = {"Authorization": "Bearer session-token", "anthropic-version": "2023-06-01", "Host": "10.213.0.1"}
    await model_proxy.open_model_request("POST", "v1/messages", "beta=true", headers, b"{}")
    assert sent["url"] == "https://api.deepseek.com/anthropic/v1/messages?beta=true"
    assert sent["headers"] == {"anthropic-version": "2023-06-01", "Authorization": "Bearer sk-ds-real"}
    with pytest.raises(model_proxy.ModelPathRefused):  # with our key, a provider's other endpoints are off limits
        await model_proxy.open_model_request("GET", "v1/files", "", headers, b"")


def test_the_agent_routes_are_never_on_the_public_api():
    # Not even reached: the public app has no such routes (no token check, no proxying -- a plain 404).
    client = TestClient(app)
    assert (
        client.post("/api/agent/model/v1/messages", headers={"Authorization": "Bearer x"}, json={}).status_code == 404
    )
    assert client.post("/api/agent/messages", headers={"Authorization": "Bearer x"}, json={}).status_code == 404
    assert any("/agent/model" in route.path for route in agent_router.routes)
