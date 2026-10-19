# Intentions: the token a conversation's agent session uses to call back into us (its channel's
# messages, its tools). It only ever speaks for THAT conversation -- so whatever the agent can reach with
# it (the MCP holds it, and so can the agent itself: same user, same env) stays inside its own project.
# It also works the other way: we send it with every push into the session (sessions.send) and its
# MCP's inbox refuses anything without it, so knowing a session's port isn't enough to talk to it.
#
# Shape: "<conversation_id>.<hmac>", the HMAC being SHA-256 over the id, keyed by SESSION_TOKEN_SECRET.
# Nothing is stored: we recompute the HMAC to check one. Gotcha: tokens never expire, and rotating the
# secret invalidates every running session's token (they get a fresh one when restarted).

import hashlib
import hmac

from src.config import settings


class SessionTokenSecretMissing(Exception):
    """SESSION_TOKEN_SECRET isn't set, so no session can be given a token (and none is ever accepted)."""


def to_session_token(conversation_id: int) -> str:
    """[Pure-ish] The token for the conversation's session. Raises SessionTokenSecretMissing if there's
    no secret to sign with."""
    if not settings.session_token_secret:
        raise SessionTokenSecretMissing("set SESSION_TOKEN_SECRET to start agent sessions")
    return f"{conversation_id}.{_sign(conversation_id)}"


def get_token_conversation_id(token: str) -> int | None:
    """[Pure-ish] The conversation a session token speaks for, or None if it isn't a valid one. Fail
    closed: with no secret configured, nothing is valid."""
    conversation_id, _, signature = token.partition(".")
    if not settings.session_token_secret or not conversation_id.isdigit():
        return None
    return int(conversation_id) if hmac.compare_digest(signature, _sign(int(conversation_id))) else None


def _sign(conversation_id: int) -> str:
    return hmac.new(
        settings.session_token_secret.encode(), f"conversation:{conversation_id}".encode(), hashlib.sha256
    ).hexdigest()
