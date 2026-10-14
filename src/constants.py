# All enums/constants should live here unless there is a very good reason for them to stay local
# to where they are used.

from enum import StrEnum


class UserType(StrEnum):
    USER = "USER"  # a real, credentialed account
    GUEST = "GUEST"  # anonymous account minted on first visit; promoted to USER on signup


class UserPlatform(StrEnum):
    """users.platform -- how an account signs in; users.platform_id is the id on that platform."""

    WEB = "WEB"  # email (username) + password; platform_id = the username
    GOOGLE = "GOOGLE"  # Google OIDC; platform_id = the `sub`


class Charset(StrEnum):
    ALPHA_NUMERIC = "abcdefghijklmnopqrstuvwxyz0123456789"
    ALPHABET = "abcdefghijklmnopqrstuvwxyz"
    NUMERIC = "0123456789"


# The httpOnly cookie carrying the user's access token (utils/auth) -- the session, Passport-style.
ACCESS_TOKEN_COOKIE = "access_token"


class GoogleLoginError(StrEnum):
    """Codes on the FE redirect when Google login fails: FRONTEND_URL/login?error=<code>."""

    DISABLED = "GOOGLE_LOGIN_DISABLED"  # client id/secret or session secret not configured
    ACCESS_DENIED = "ACCESS_DENIED"  # the user cancelled on Google's consent screen
    INVALID_STATE = "INVALID_STATE"  # state missing/mismatched -- stale tab, replay, or forgery
    EMAIL_NOT_VERIFIED = "EMAIL_NOT_VERIFIED"
    EMAIL_TAKEN = "EMAIL_TAKEN"  # a new Google account's email is already registered (no merging)
    GOOGLE_AUTH_FAILED = "GOOGLE_AUTH_FAILED"  # any other OAuth failure (code exchange, id token check)
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ConfigStatus(StrEnum):
    """system_configurations.status -- whether a setting is in EFFECT. Orthogonal to is_private
    (which controls who may READ it)."""

    ENABLED = "ENABLED"  # the row's value is in effect
    DISABLED = "DISABLED"  # treated as absent -> callers fall back to their code default


class ConfigName(StrEnum):
    """The keys that live in the system_configurations table. Declare each one here rather than
    scattering raw strings at the call sites, so there's one list of what's tunable at runtime.
    Empty in the boilerplate -- add yours as the app grows."""


# --- Agent sessions (services/agent/adapter) ---
# A project's agent session is a long-running process found again by its working directory (the
# project's workspace) plus this env var, which also carries the port it listens on.
SESSION_PORT_ENV = "EVENT_CHANNEL_PORT"
# Where the session's event-channel MCP posts the agent's messages back (POST /api/agent/{port}/messages)
# and the bearer token it sends. Both are handed to the session at launch (services/agent/adapter).
SESSION_OUTBOX_URL_ENV = "EVENT_CHANNEL_OUTBOX_URL"
SESSION_OUTBOX_TOKEN_ENV = "EVENT_CHANNEL_OUTBOX_TOKEN"
# What a session runs (argv, no shell), launched inside the project's workspace with SESSION_PORT_ENV set.
# SESSION_COMMAND = ["doit", "--param", "1"]
SESSION_COMMAND = ["claude", "--dangerously-load-development-channels", "server:event-channel"]
# Claude's startup screens that need an answer before the session runs. start_session watches the
# session's terminal and answers them, giving up after the wait below:
#   - the folder-trust dialog, shown in a folder Claude hasn't been told to trust (a trusted parent folder
#     counts). Its preselected option is "No, exit", so it's answered with Down then Enter.
#   - the --dangerously-load-development-channels confirmation; its option 1 (this text) is preselected,
#     so Enter accepts it. This one is always last.
SESSION_TRUST_PROMPT = "Yes, I trust this folder"
SESSION_STARTUP_PROMPT = "I am using this for local development"
SESSION_STARTUP_PROMPT_WAIT_SECONDS = 30
# How long a freshly picked port stays reserved -- covers the gap until the session actually binds it.
SESSION_PORT_LOCK_TTL_SECONDS = 300
SESSION_PORT_MAX_ATTEMPTS = 1000
# Per-project mutex around find-or-start, so two concurrent calls can't both start a session.
# Generous, since starting a session now includes waiting out its startup prompt (below).
SESSION_LOCK_TTL_SECONDS = 120
SESSION_LOCK_WAIT_SECONDS = 60
# Attempts at pushing a message into a session (services/agent/sessions.send), with backoff growing to 2s
# -- about 15s in all, enough for a session that was just started to begin listening.
SESSION_SEND_ATTEMPTS = 10


# Whether user messages are answered by the stand-in agent (services/agent/mock) instead of being sent to
# the project's real agent session. Flip it off once sending to the session is built.
AGENT_MOCK_REPLIES = False

# Per-project mutex around "find or create the current conversation" (services/agent/conversations).
AI_CONVERSATION_LOCK_TTL_SECONDS = 10
AI_CONVERSATION_LOCK_WAIT_SECONDS = 5
# Attempts at claiming a message's next seq (repositories/ai_messages). Only concurrent writers to the
# SAME conversation collide -- normally a session message vs. a user answer, two at most.
AI_MESSAGE_SEQ_ATTEMPTS = 10


class AiMessageRole(StrEnum):
    """ai_messages.role -- the LLM API's two turn roles. A user's answers to the agent's questions /
    actions are USER messages too (tool_result blocks), same as in the API."""

    USER = "user"
    ASSISTANT = "assistant"


# The user-facing tool the agent asks multiple-choice questions with. Its input is {"questions": [...]};
# the answer comes back as a tool_result whose content is the array of per-question results.
ASK_USER_TOOL_NAME = "ask_user"
# The tool the agent shows the user a file with: input {"path": "artifacts/<name>", "title": "..."}. Not
# user-facing -- it never waits on the user; the FE just renders it as a button opening the file.
SHOW_ARTIFACT_TOOL_NAME = "show_artifact"
# The tool the agent proposes externally declared actions with: input {"actions": [{name, params}]}. The
# session's channel sends these; the FE can't answer them yet.
REQUEST_ACTIONS_TOOL_NAME = "request_actions"
# Tools whose "execution" is the user answering. A tool_use of one of these pauses the conversation
# until a matching tool_result arrives (services/agent/interactions).
USER_FACING_TOOLS = [ASK_USER_TOOL_NAME]


class InteractionStatus(StrEnum):
    """Per-item status in a submission. An array is either all SUBMITTED, or SUBMITTED* then exactly one
    ABORTED then UNTOUCHED* -- the user works through items in order and bails out at one."""

    SUBMITTED = "submitted"
    ABORTED = "aborted"
    UNTOUCHED = "untouched"  # never reached, because an earlier item was aborted


# Pages of GET /conversations/{id}/messages (infinite scroll up).
MESSAGES_PAGE_DEFAULT = 50
MESSAGES_PAGE_MAX = 200


# File attachments (POST /conversations/{id}/uploads). Per request; the real body-size guard belongs in
# nginx too (client_max_body_size), since the multipart body is parsed before our code sees it.
UPLOAD_MAX_FILES = 10
UPLOAD_MAX_BYTES = 25 * 1024 * 1024  # per file


# How long a server reload/shutdown waits for open connections before closing them (main.py). Has to be
# short: SSE streams never finish on their own before SSE_MAX_AGE_SECONDS.
SERVER_GRACEFUL_SHUTDOWN_SECONDS = 3


# SSE (GET /conversations/{id}/events). With nothing new, the stream sends a heartbeat comment this
# often -- keeps proxies from closing it as idle, and doubles as the fallback db check for a missed
# wake-up. Each stream is closed after the max age; the browser reconnects at once, which re-checks auth.
SSE_HEARTBEAT_SECONDS = 15
SSE_MAX_AGE_SECONDS = 1800
# How long EventSource waits before reconnecting after a drop, in ms (sent once, up front).
SSE_RETRY_MS = 3000
