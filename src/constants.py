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


class UserLanguage(StrEnum):
    """users.language -- the language the user picked in the FE's language picker (keep in sync with its
    SUPPORTED_LANGUAGES). The UI shows in it, and the agent talks to them in it."""

    EN = "en"
    VI = "vi"


# How the agent is told about a language (services/agent/sessions.introduce_project).
USER_LANGUAGE_NAMES = {UserLanguage.EN: "English", UserLanguage.VI: "Vietnamese"}


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
# The MCP server each session runs to talk to us (src/mcps/communication): its name in the session's
# MCP config, which is also what SESSION_COMMAND loads as a channel.
COMMUNICATION_MCP_NAME = "communication"


# A project's agent session is a long-running process found again by its working directory (the
# project's workspace) plus this env var, which also carries the port it listens on.
SESSION_PORT_ENV = "COMMUNICATION_PORT"
# Where the session's communication MCP posts the agent's messages back (POST /api/agent/messages)
# and the bearer token it sends. Both are handed to the session at launch (services/agent/adapter).
SESSION_OUTBOX_URL_ENV = "COMMUNICATION_OUTBOX_URL"
SESSION_OUTBOX_TOKEN_ENV = "COMMUNICATION_OUTBOX_TOKEN"
# What a session runs (argv, no shell), launched inside the project's workspace with SESSION_PORT_ENV set.
# SESSION_COMMAND = ["doit", "--param", "1"]
# --permission-mode auto: nobody watches the session's terminal, so it must never stop to ask before a tool
# use (it would wait forever). Auto mode decides on its own -- risky actions are blocked rather than asked
# about. Set explicitly: otherwise the mode depends on the machine's Claude account settings.
SESSION_COMMAND = [
    "claude",
    "--dangerously-load-development-channels",
    f"server:{COMMUNICATION_MCP_NAME}",
    "--permission-mode",
    "auto",
]
# Added to SESSION_COMMAND when the workspace already has a Claude conversation, so a restarted session
# picks up where the last one left off (same flags every time: they're not saved with the conversation).
# Only then: with nothing to continue, `claude --continue` exits straight away. A workspace is meant to
# hold exactly one conversation, so "the most recent" is the one.
SESSION_CONTINUE_ARGS = ["--continue"]
# Where Claude keeps its conversations: one folder per working directory, named after its path with
# every non-alphanumeric character turned into "-", holding a <session id>.jsonl transcript each.
CLAUDE_PROJECTS_DIR = "~/.claude/projects"
# Claude's startup screens that need an answer before the session runs. start_session watches the
# session's terminal and answers them, giving up after the wait below:
#   - the folder-trust dialog, shown in a folder Claude hasn't been told to trust (a trusted parent folder
#     counts). Its preselected option is "No, exit", so it's answered with Down then Enter.
#   - the --dangerously-load-development-channels confirmation; its option 1 (this text) is preselected,
#     so Enter accepts it. This one is always last.
SESSION_TRUST_PROMPT = "Yes, I trust this folder"
SESSION_STARTUP_PROMPT = "I am using this for local development"
SESSION_STARTUP_PROMPT_WAIT_SECONDS = 30
# Sessions listen on a random port from this range (inclusive). The ranges below it belong to the
# projects' apps, one fixed port each: dev backend 8000 + project id, dev frontend 16000 + id, prod
# backend 24000 + id -- so a server fits ids below 8000. Everything else (db, redis, our API) stays
# below 8000, and all of it stays below the OS's outgoing-connection ports (32768+), so nothing can
# squat a project's port.
SESSION_PORT_RANGE = (40000, 60000)
# How long a freshly picked port stays reserved -- covers the gap until the session actually binds it.
SESSION_PORT_LOCK_TTL_SECONDS = 300
SESSION_PORT_MAX_ATTEMPTS = 1000
# Per-project mutex around find-or-start, so two concurrent calls can't both start a session.
# Generous, since starting a session now includes waiting out its startup prompt (below).
SESSION_LOCK_TTL_SECONDS = 120
SESSION_LOCK_WAIT_SECONDS = 60
# How long stopping a session waits for its processes to exit after SIGTERM, before it SIGKILLs them.
SESSION_STOP_WAIT_SECONDS = 5
# Attempts at pushing a message into a session (services/agent/sessions.send), with backoff growing to 2s
# -- about 15s in all, enough for a session that was just started to begin listening.
SESSION_SEND_ATTEMPTS = 10

# The only variables of our environment that commands we launch get to see (utils/processes) -- the
# agent's tmux session, uv, npm, pm2. Everything else stays behind: python-dotenv loads our .env files
# INTO our environment, so passing it on would hand our secrets to code the agent writes, and our config
# (API_PORT...) would override the project's own .env files.
SUBPROCESS_ENV_PASSTHROUGH = [
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM", "TMPDIR", "TMUX_TMPDIR",
    "XDG_RUNTIME_DIR",
]  # fmt: skip

# --- Dev environments (services/devenv) ---
# A project's dev servers listen on these + its id (see SESSION_PORT_RANGE above for the whole plan).
PROJECT_DEV_BACKEND_PORT_BASE = 8000
PROJECT_DEV_FRONTEND_PORT_BASE = 16000
# Each project's own Redis databases (on PROJECT_REDIS_URL's server): dev = base + id, prod = base + id.
# A whole database per project rather than key prefixes, so projects can't see each other's keys whatever
# the generated code does with Redis. db 0 is the platform's. The bases start at 1000 so a Redis server
# shared with other apps keeps 1-999 for them (on the VPS, the gx projects use db = their id, 1-255).
# With ids below 8000 that's databases 1001-17999, so the server needs PROJECT_REDIS_DATABASES_NEEDED.
PROJECT_REDIS_DEV_DB_BASE = 1000
PROJECT_REDIS_PROD_DB_BASE = 10000
PROJECT_REDIS_DATABASES_NEEDED = 18000
# ...except on a local platform (ENV=local), where every project uses the same two fixed ports and there's
# no nginx: open the dev site straight on the frontend port. So only one project's dev servers can run
# at a time locally -- another project's would find the ports taken.
LOCAL_DEV_BACKEND_PORT = 10101
LOCAL_DEV_FRONTEND_PORT = 10102
# The boilerplate's apps, copied into the workspace when implementing starts (projects.stage records
# that it started; these being there means the setup got that far).
DEVENV_APP_DIRS = ["backend", "frontend"]
# Never copied from the boilerplate, wherever they appear: VCS, dependencies, build output, caches, logs,
# and env files (the project's own are rendered from DEVENV_ENV_FILES instead).
DEVENV_COPY_SKIPPED_NAMES = [
    ".git", "node_modules", ".venv", "dist", "logs", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".env", ".env.local", ".env.dev", ".env.prod",
]  # fmt: skip
# (template in DEVENV_TEMPLATES_DIR, output relative to the workspace). Written once, never overwritten:
# they hold secrets in use, and the agent may have edited them since.
DEVENV_ENV_FILES = [
    ("backend.env.local.tmpl", "backend/.env.local"),
    ("frontend.env.tmpl", "frontend/.env"),
]
# Ours, not the agent's, so these are re-rendered every time: the pm2 file (in the workspace) and the
# nginx site (in NGINX_SITES_DIR).
DEVENV_ECOSYSTEM_TEMPLATE = ("ecosystem.config.cjs.tmpl", "ecosystem.config.cjs")
DEVENV_NGINX_TEMPLATE = "nginx-dev.conf.tmpl"
# What the dev backend answers once it's up; checked after the servers start.
DEVENV_BACKEND_HEALTH_PATH = "/api/debug/health"
DEVENV_HEALTH_WAIT_SECONDS = 90
# Per-conversation mutex around setting up. Generous: a first setup runs uv sync and npm install. A
# second caller waits it out and then re-runs the (by then quick) setup, so it gets a real answer too.
DEVENV_LOCK_TTL_SECONDS = 900
DEVENV_LOCK_WAIT_SECONDS = 900
DEVENV_INSTALL_TIMEOUT_SECONDS = 600
# The payload of a conversation's wake-up (services/agent/conversations) that means "its dev environment
# changed" rather than "new messages" -- the SSE stream turns it into an `event: devenv` hint.
DEVENV_WAKEUP = "devenv"


class ProjectStage(StrEnum):
    """projects.stage -- where a project is in the building process. Moves forward only (see
    migrations/0005_projects.sql). The starting form before it is the FE's alone: a project only exists
    once that's submitted."""

    SPECING = "specing"  # chatting with the agent to work out the spec; no app yet
    IMPLEMENTING = "implementing"  # the app is set up (or being set up): there's something to preview


class DevServerAction(StrEnum):
    """What the agent can ask of its project's dev servers (POST /agent/dev-servers)."""

    STATUS = "status"
    START = "start"
    RESTART = "restart"
    STOP = "stop"


# Whether user messages are answered by the stand-in agent (services/agent/mock) instead of being sent to
# the project's real agent session. Flip it off once sending to the session is built.
AGENT_MOCK_REPLIES = False

# The ai_conversations.meta key marking that the agent was handed the project's starting form
# (services/agent/sessions.ensure_introduced) -- set once, by whichever open gets there first.
CONVERSATION_INTRODUCED_KEY = "introduced_at"

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


# How long the agent's "building" flag lasts (services/agent/building) unless it clears or refreshes it --
# so a flag the agent forgot about can't leave a project "building" forever.
BUILDING_TTL_SECONDS = 3600

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
