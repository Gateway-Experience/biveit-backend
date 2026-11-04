# Intentions: product analytics events to PostHog, from the backend. Generic: it knows nothing about what an event
# means -- callers pick the event (constants.AnalyticsEvent) and its properties. Connection-module convention (see
# utils/postgres): connect() once at startup, close() at shutdown. With no POSTHOG_KEY it's all a no-op, so callers
# never have to guard.
# capture() doesn't block or raise: the SDK queues events and a background thread sends them in batches.
# References: biveit-frontend services/analytics (the browser's half -- same person id, the user id as a string).

from typing import Any

from posthog import Posthog

from src.config import settings

_client: Posthog | None = None

# Set by the browser SDK on our API calls once the visitor allowed analytics (its `tracing_headers` option).
_SESSION_ID_HEADER = "x-posthog-session-id"


def connect() -> None:
    global _client
    if _client is None and settings.posthog_key:
        _client = Posthog(settings.posthog_key, host=settings.posthog_host)


def close() -> None:
    """Sends what's still queued, then stops the sender thread."""
    global _client
    if _client is not None:
        _client.shutdown()
        _client = None


def capture(
    user_id: int, event: str, properties: dict[str, Any] | None = None, options: dict[str, Any] | None = None
) -> None:
    """Sends `event` for the user (the browser identifies people by the same id, as a string).

    options:
      - session_id: the browser session it happened in (see read_session_id) -- ties it to that session's replay.
      - set_once: person properties written only if they're not set yet (e.g. how they signed up).
      - uuid: a fixed id for an event that might be sent twice (webhooks retry); PostHog keeps one of them.
    """
    if _client is None:
        return
    opts = options or {}
    _client.capture(
        event,
        distinct_id=str(user_id),
        properties={
            **(properties or {}),
            **({"$session_id": opts["session_id"]} if opts.get("session_id") else {}),
            **({"$set_once": opts["set_once"]} if opts.get("set_once") else {}),
        },
        **({"uuid": opts["uuid"]} if opts.get("uuid") else {}),
    )


def read_session_id(headers: Any) -> str | None:
    """[Pure] The PostHog session id a browser request carries, if any. Takes the request's headers (any
    case-insensitive mapping)."""
    return headers.get(_SESSION_ID_HEADER) or None
