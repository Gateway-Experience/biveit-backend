# The backend's analytics events (utils/analytics + where each milestone sends one). The PostHog client is swapped for
# a recorder, so what's under test is: when an event goes out (once, at the moment the fact becomes true) and what it
# carries -- not PostHog itself.

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src.app import create_app
from src.constants import AiMessageRole, AnalyticsEvent
from src.modules.payments.constants import Provider, SubscriptionStatus
from src.modules.payments.services import purchases
from src.services import users as user_service
from src.services.agent import conversations
from src.utils import analytics

_CREATED_AT = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
_PROJECT = {"id": 7, "user_id": 3, "created_at": _CREATED_AT}


@pytest.fixture
def captured(monkeypatch):
    """Records what's sent instead of sending it; returns the list of (event, kwargs)."""
    sent = []

    class _Recorder:
        def capture(self, event, **kwargs):
            sent.append((event, kwargs))

    monkeypatch.setattr(analytics, "_client", _Recorder())
    return sent


# --- the client ---


def test_nothing_is_sent_without_a_key(monkeypatch):
    monkeypatch.setattr(analytics, "_client", None)
    analytics.capture(3, AnalyticsEvent.PLAN_APPROVED, {"project_id": 7})  # no client, no error


def test_capture_sends_the_user_id_as_a_string_with_its_options(captured):
    analytics.capture(
        3,
        AnalyticsEvent.USER_SIGNED_UP,
        {"method": "email"},
        {"session_id": "s-1", "set_once": {"signup_method": "email"}, "uuid": "u-1"},
    )
    assert captured == [
        (
            "user_signed_up",
            {
                "distinct_id": "3",
                "properties": {"method": "email", "$session_id": "s-1", "$set_once": {"signup_method": "email"}},
                "uuid": "u-1",
            },
        )
    ]


def test_the_session_id_is_read_from_the_browsers_header():
    assert analytics.read_session_id({"x-posthog-session-id": "s-1"}) == "s-1"
    assert analytics.read_session_id({}) is None


# --- agent_first_replied ---


@pytest.mark.parametrize(("first_said_id", "is_sent"), [(11, True), (10, False), (None, False)])
async def test_the_first_reply_is_sent_only_for_the_first_thing_said(monkeypatch, captured, first_said_id, is_sent):
    message = {"id": 11, "conversation_id": 5, "created_at": _CREATED_AT + timedelta(seconds=42)}

    async def get_first_said(conversation_id, role):
        assert (conversation_id, role) == (5, AiMessageRole.ASSISTANT)
        return {"id": first_said_id} if first_said_id else None

    monkeypatch.setattr(conversations.ai_messages_repo, "get_first_said", get_first_said)
    await conversations._capture_if_first_reply(_PROJECT, message)
    expected = [
        ("agent_first_replied", {"distinct_id": "3", "properties": {"project_id": 7, "seconds_since_created": 42}})
    ]
    assert captured == (expected if is_sent else [])


# --- payment_succeeded ---


def _purchase(status):
    return {
        "provider": Provider.POLAR_SANDBOX,
        "provider_object": "subscription",
        "provider_ref": "sub_1",
        "user_id": 3,
        "target_id": 7,
        "product": "plan",
        "plan": "yearly",
        "status": status,
    }


@pytest.mark.parametrize(
    ("previous_status", "status", "is_sent"),
    [
        (None, SubscriptionStatus.ACTIVE, True),  # first sight, already paid
        (SubscriptionStatus.PENDING, SubscriptionStatus.ACTIVE, True),  # paid just now
        (None, SubscriptionStatus.TRIALING, True),  # a domain's free first year: bought
        (None, SubscriptionStatus.PENDING, False),  # not paid yet
        (SubscriptionStatus.ACTIVE, SubscriptionStatus.ACTIVE, False),  # another webhook about the same purchase
        (SubscriptionStatus.CANCELLED, SubscriptionStatus.ACTIVE, False),  # un-cancelled, not a new purchase
        (SubscriptionStatus.PAST_DUE, SubscriptionStatus.ACTIVE, False),  # a renewal that went through after all
    ],
)
async def test_a_payment_is_sent_once_when_it_gets_paid(monkeypatch, captured, previous_status, status, is_sent):
    async def find(criteria):
        assert criteria == {
            "provider": Provider.POLAR_SANDBOX,
            "provider_object": "subscription",
            "provider_ref": "sub_1",
        }
        return [_purchase(previous_status)] if previous_status else []

    async def upsert(row):
        return row

    monkeypatch.setattr(purchases.subscriptions_repo, "find", find)
    monkeypatch.setattr(purchases.subscriptions_repo, "upsert", upsert)
    await purchases._save_purchase(_purchase(status))
    assert [event for event, _ in captured] == (["payment_succeeded"] if is_sent else [])
    if is_sent:
        ((_, kwargs),) = captured
        assert kwargs["properties"] == {
            "project_id": 7,
            "product": "plan",
            "plan": "yearly",
            "status": status,
            "sandbox": True,
        }


async def test_a_payment_event_has_the_same_uuid_however_often_its_sent(monkeypatch, captured):
    async def find(criteria):
        return []

    async def upsert(row):
        return row

    monkeypatch.setattr(purchases.subscriptions_repo, "find", find)
    monkeypatch.setattr(purchases.subscriptions_repo, "upsert", upsert)
    await purchases._save_purchase(_purchase(SubscriptionStatus.ACTIVE))
    await purchases._save_purchase(_purchase(SubscriptionStatus.ACTIVE))
    first_uuid, second_uuid = [kwargs["uuid"] for _, kwargs in captured]
    assert first_uuid == second_uuid


# --- user_signed_up (email) ---


def test_an_email_signup_is_sent_with_the_browsers_session(monkeypatch, captured):
    async def register(username, password, user_data):
        return {"user": {"id": 3, "username": username}, "token": "tok"}

    monkeypatch.setattr(user_service, "register", register)
    client = TestClient(create_app())
    response = client.post(
        "/api/user/register",
        json={"username": "a@b.c", "password": "long-enough-1", "first_name": "A"},
        headers={"X-POSTHOG-SESSION-ID": "s-1"},
    )
    assert response.status_code == 200
    assert captured == [
        (
            "user_signed_up",
            {
                "distinct_id": "3",
                "properties": {
                    "method": "email",
                    "was_guest": False,
                    "$session_id": "s-1",
                    "$set_once": {"signup_method": "email"},
                },
            },
        )
    ]
