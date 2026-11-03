# Email sending tests (utils/email). The HTTP pool is monkeypatched, so what's under test is what we hand Resend
# and how its answers come back: an id on success, EmailNotSent on a refusal or a missing sender.

import pytest

from src.config import settings
from src.utils import email, http


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "email_from", "biveit <no-reply@mail.biveit.com>")
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")


def _fake_resend(monkeypatch, status, body):
    calls = []

    async def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return status, body, {}

    monkeypatch.setattr(http, "request", fake_request)
    return calls


async def test_an_email_goes_to_resend_from_our_sender(configured, monkeypatch):
    calls = _fake_resend(monkeypatch, 200, {"id": "email-1"})

    sent_id = await email.send_email("alice@example.com", "Reset", "the text", "<p>the html</p>")

    assert sent_id == "email-1"
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://api.resend.com/emails"
    assert calls[0]["headers"] == {"Authorization": "Bearer re_test_key"}
    assert calls[0]["json"] == {
        "from": "biveit <no-reply@mail.biveit.com>",
        "to": ["alice@example.com"],
        "reply_to": "support@biveit.com",
        "subject": "Reset",
        "text": "the text",
        "html": "<p>the html</p>",
    }


async def test_a_text_only_email_sends_no_html(configured, monkeypatch):
    calls = _fake_resend(monkeypatch, 200, {"id": "email-2"})

    await email.send_email("alice@example.com", "Reset", "the text")

    assert "html" not in calls[0]["json"]


async def test_a_refusal_raises(configured, monkeypatch):
    _fake_resend(monkeypatch, 403, {"message": "The mail.biveit.com domain is not verified."})

    with pytest.raises(email.EmailNotSent, match="not verified"):
        await email.send_email("alice@example.com", "Reset", "the text")


@pytest.mark.parametrize(("email_from", "api_key"), [("", "re_test_key"), ("biveit <no-reply@mail.biveit.com>", "")])
async def test_no_sender_raises_without_calling_resend(monkeypatch, email_from, api_key):
    monkeypatch.setattr(settings, "email_from", email_from)
    monkeypatch.setattr(settings, "resend_api_key", api_key)
    calls = _fake_resend(monkeypatch, 200, {"id": "never"})

    with pytest.raises(email.EmailNotSent):
        await email.send_email("alice@example.com", "Reset", "the text")
    assert calls == []
