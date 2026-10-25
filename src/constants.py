# All enums/constants should live here unless there is a very good reason for them to stay local
# to where they are used.

from enum import StrEnum

from session_runner import protocol as _runner_protocol

# The protocol between the platform and its session runner (the biveit-session-runner repo): ONE source for
# both sides, taken over here under the names the backend has always used. See session_runner/protocol.py.
SESSION_OUTBOX_URL_ENV = _runner_protocol.OUTBOX_URL_ENV
SESSION_TOKEN_ENV = _runner_protocol.TOKEN_ENV
RUNNER_SOCKET_ENV = _runner_protocol.SOCKET_ENV
AiSignal = _runner_protocol.AiSignal
AGENT_SIGNALS = _runner_protocol.AGENT_SIGNALS
RUNNER_SIGNALS = _runner_protocol.RUNNER_SIGNALS
AgentAction = _runner_protocol.AgentAction
ApprovalDecision = _runner_protocol.ApprovalDecision
InteractionStatus = _runner_protocol.InteractionStatus
DevServerAction = _runner_protocol.DevServerAction
TurnState = _runner_protocol.TurnState


class UserType(StrEnum):
    USER = "USER"  # a real, credentialed account
    GUEST = "GUEST"  # anonymous account minted on first visit; promoted to USER on signup


class UserRole(StrEnum):
    """users.role -- what an account may do beyond being a user. A normal user has none (NULL)."""

    ADMIN = "ADMIN"  # the platform's operators: the admin dashboard (controllers/admin)


class UserPlatform(StrEnum):
    """users.platform -- how an account signs in; users.platform_id is the id on that platform."""

    WEB = "WEB"  # email (username) + password; platform_id = the username
    GOOGLE = "GOOGLE"  # Google OIDC; platform_id = the `sub`


class UserLanguage(StrEnum):
    """users.language -- the language the user picked in the FE's language picker (keep in sync with its
    SUPPORTED_LANGUAGES). The UI shows in it, and the agent talks to them in it."""

    EN = "en"
    VI = "vi"


class UserJourney(StrEnum):
    """The flows the FE walks a user through step by step. The steps they take are logged in
    user_journey_steps (see its migration); which steps there are is the FE's call, not ours."""

    # The first-time intro on the projects page, for accounts that have never made a project.
    PROJECTS_INTRO = "projects_intro"


# A journey step's name: the column's varchar limit (migrations/0009_user_journey_steps.sql).
JOURNEY_STEP_MAX_LENGTH = 100


# How the agent is told about a language (services/agent/sessions.introduce_project).
USER_LANGUAGE_NAMES = {UserLanguage.EN: "English", UserLanguage.VI: "Vietnamese"}


class Charset(StrEnum):
    ALPHA_NUMERIC = "abcdefghijklmnopqrstuvwxyz0123456789"
    ALPHABET = "abcdefghijklmnopqrstuvwxyz"
    NUMERIC = "0123456789"


# The httpOnly cookie carrying the user's access token (utils/auth) -- the session, Passport-style.
ACCESS_TOKEN_COOKIE = "access_token"

# Project search (services/projects.search_prompts): the shortest query worth running, and how much of
# the matching prompt to show around the match, in characters before / after it.
PROJECT_SEARCH_MIN_LENGTH = 2
PROJECT_SEARCH_EXCERPT_CONTEXT = (40, 90)

# Profile pictures uploaded from the profile page (services/users/avatars). profile_picture then holds
# AVATAR_URL_PREFIX/<user hash>/<file>, a path on this API (the FE puts its API base in front); Google's
# pictures are full URLs, so the prefix also tells the two apart.
AVATAR_MAX_BYTES = 5 * 1024 * 1024
AVATAR_URL_PREFIX = "/avatars"
AVATAR_DIR = "profile"  # inside the user's storage folder

# A user's first / last name: the columns' varchar limit (migrations/0001_users.sql).
USER_NAME_MAX_LENGTH = 100

# Password rules + reset. The FE mirrors MIN_PASSWORD_LENGTH (biveit-frontend authForm.ts).
MIN_PASSWORD_LENGTH = 8
PASSWORD_RESET_TOKEN_TTL_SECONDS = 30 * 60
# One reset email per address per window, so the endpoint can't be used to flood someone's inbox.
PASSWORD_RESET_THROTTLE_SECONDS = 60
# The FE page the emailed link opens, with ?token=<token> appended.
PASSWORD_RESET_FE_PATH = "/reset-password"


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


# --- The agent's runners: the Claude Agent SDK runtime (services/agent + the biveit-session-runner repo) ---
# One runner process per conversation holds its SDK session; it listens on a unix socket, told where by
# RUNNER_SOCKET_ENV (from the runner's protocol, top of this file). The runner's own tunables live in its
# repo (session_runner/constants.py).
# How long ensure_session waits for a freshly spawned runner to answer before giving up.
RUNNER_START_WAIT_SECONDS = 30
# Runners run in their project's container (services/devenv/containers), installed at this path by the
# backend (services/agent/runner_installs): root-owned, so the agent can read but not change its runner.
RUNNER_INSTALL_PATH = "/opt/session-runner"
# The backend's agent listener (src/app.py): the agent endpoints, the model proxy among them, on the
# containers' gateway -- the one host address a container can reach. Never on a public interface.
AGENT_LISTENER_PORT = 3002
# The model proxy (services/agent/model_proxy): where it forwards when ANTHROPIC_BASE_URL isn't set, the only
# paths it forwards, and how long it waits for the next bit of a streamed answer.
MODEL_PROXY_DEFAULT_BASE_URL = "https://api.anthropic.com"
MODEL_PROXY_PATHS = ["v1/messages", "v1/messages/count_tokens"]
MODEL_PROXY_READ_TIMEOUT_SECONDS = 600
# How long the backend waits on any one request to a runner (they all answer right away) before calling it hung.
RUNNER_REQUEST_TIMEOUT_SECONDS = 10
# How Claude Code runs in the runners against another endpoint (settings.anthropic_base_url), as the env
# vars it reads -- applied only then; on Anthropic's own API, Claude Code's defaults apply. Now: DeepSeek.
#   models etc.               per DeepSeek's Claude Code guide ([1m] = the 1M-token context window)
#   CLAUDE_CODE_INLINE_TOOLS  off: newer Claude Code declares tools inline (tool_definition blocks), which
#                             DeepSeek can't parse -- it answers 422, which Claude Code doesn't recognise as
#                             "unsupported", so it never falls back on its own
AGENT_ENDPOINT_ENV = {
    "CLAUDE_CODE_INLINE_TOOLS": "false",
    "ANTHROPIC_MODEL": "deepseek-flash[1m]",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-flash[1m]",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-flash[1m]",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-flash",
    "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-flash",
    "CLAUDE_CODE_EFFORT_LEVEL": "max",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "950000",
}
# The model key sets scripts/manage.py switches between: one .env-format file per set in this folder (at the
# repo root, gitignored), each holding only these variables. Applying a set rewrites all of them in
# .env.<env>, so nothing of the previous provider lingers.
AGENT_API_KEYS_DIR = ".agent_api_keys"
AGENT_API_KEY_NAMES = ["ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"]

# Attempts at pushing a message into a runner (services/agent/runners.send), with backoff growing to 2s
# -- about 15s in all, enough for a runner that was just started to begin listening.
RUNNER_SEND_ATTEMPTS = 10

# The only variables of our environment that commands we launch get to see (utils/processes) -- docker,
# git, uv, npm. Everything else stays behind: python-dotenv loads our .env files
# INTO our environment, so passing it on would hand our secrets to code the agent writes, and our config
# (API_PORT...) would override the project's own .env files.
SUBPROCESS_ENV_PASSTHROUGH = [
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM", "TMPDIR",
    "XDG_RUNTIME_DIR",
]  # fmt: skip

# --- Dev environments (services/devenv) ---
# A project's dev servers are published on the host's 127.0.0.1 at these + its id (so ids below 8000 fit):
# dev backend 8000 + id, dev frontend 16000 + id, prod backend 24000 + id. Everything else (db, redis, our
# API) stays below 8000, and all of it below the OS's outgoing-connection ports (32768+).
PROJECT_DEV_BACKEND_PORT_BASE = 8000
PROJECT_DEV_FRONTEND_PORT_BASE = 16000
# ...except on a local platform (ENV=local), where every project uses the same two fixed ports and there's
# no nginx: open the dev site straight on the frontend port. So only one project's dev servers can run
# at a time locally -- another project's would find the ports taken.
LOCAL_DEV_BACKEND_PORT = 10101
LOCAL_DEV_FRONTEND_PORT = 10102
# The boilerplate's apps, copied into the workspace when implementing starts (projects.stage records
# that it started; these being there means the setup got that far).
DEVENV_APP_DIRS = ["backend", "frontend"]
# Never copied from the boilerplate, wherever they appear: VCS, dependencies, build output, caches, logs,
# and env files (the project's own are rendered from DEVENV_ENV_FILES instead) -- the env templates too: they
# stay in the boilerplate, which is the only place they're ever rendered from.
DEVENV_COPY_SKIPPED_NAMES = [
    ".git", "node_modules", ".venv", "dist", "logs", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".env", ".env.local", ".env.dev", ".env.prod", ".env.local.tmpl", ".env.tmpl",
]  # fmt: skip
# The project's env files: (template, relative to BOILERPLATE_DIR -- each app keeps its own, since it knows
# its own config keys; output, relative to the workspace). Written once, never overwritten: they hold
# secrets in use, and the agent may have edited them since. Rendered from the BOILERPLATE, never from a
# workspace's copy: templates run their expressions (templates.py), and a workspace is the agent's.
DEVENV_ENV_FILES = [
    ("backend/.env.local.tmpl", "backend/.env.local"),
    ("frontend/.env.tmpl", "frontend/.env"),
]
# Ours, not the agent's, so these are re-rendered every time: the pm2 file (in the workspace) and the
# nginx site (in NGINX_SITES_DIR).
DEVENV_ECOSYSTEM_TEMPLATE = ("ecosystem.config.cjs.tmpl", "ecosystem.config.cjs")
DEVENV_NGINX_TEMPLATE = "nginx-dev.conf.tmpl"
# What the dev backend answers once it's up; checked after the servers start.
DEVENV_BACKEND_HEALTH_PATH = "/api/debug/health"
DEVENV_HEALTH_WAIT_SECONDS = 90
# How long the agent has to go quiet (no message, no signal) while implementing before its site is
# photographed for the project list (services/devenv/previews): its changes need a moment to show.
SITE_PREVIEW_DELAY_SECONDS = 20
# Per-conversation mutex around setting up. Generous: a first setup runs uv sync and npm install. A
# second caller waits it out and then re-runs the (by then quick) setup, so it gets a real answer too.
DEVENV_LOCK_TTL_SECONDS = 900
DEVENV_LOCK_WAIT_SECONDS = 900
DEVENV_INSTALL_TIMEOUT_SECONDS = 600
# The payload of a conversation's wake-up (services/agent/conversations) that means "its dev environment
# changed" rather than "new messages" -- the SSE stream turns it into an `event: devenv` hint.
DEVENV_WAKEUP = "devenv"

# --- Project containers (services/devenv/containers) ---
# The common image new projects' containers run (data/devenv_image, built by scripts/build_devenv_image.sh).
# Bump the tag with every Dockerfile change: a project keeps the tag it was created with (projects.meta),
# so a reused tag would change existing projects' environment under them. Pushed to Docker Hub as
# <namespace>/biveit-devenv:<tag> once the namespace is set here.
DEVENV_IMAGE = "biveit-devenv:1"
# The docker network every project's container joins; its range is DEVENV_NETWORK_SUBNET (a setting: it
# depends on what the machine's other networks use). The bridge interface is named after it too, so the
# host's firewall rules can tell the containers' traffic apart.
DEVENV_NETWORK = "biveit-projects"
# Where things sit inside every container -- the same for each project, so nothing of the host's layout
# shows (the backend translates: containers.to_container_path).
DEVENV_PROJECT_PATH = "/project"
DEVENV_SOCKETS_PATH = "/sockets"
# The container's own state, inside the project folder so it survives the container being recreated:
# HOME (Claude Code's settings and transcripts, tool caches), PM2_HOME, Redis data, logs.
DEVENV_STATE_DIR = ".devenv"
# The dev servers' ports INSIDE a container -- the same for every project (each container has its own
# network). The host side stays per project (PROJECT_DEV_BACKEND_PORT_BASE...), published on 127.0.0.1.
DEVENV_INTERNAL_BACKEND_PORT = 8000
DEVENV_INTERNAL_FRONTEND_PORT = 5173
# Ceilings for one project's container, not reservations -- it only uses what it needs.
#   memory     no swap on top (--memory-swap = memory): over the limit, the kernel kills inside it only
#   pids       counts THREADS: Chromium, Node tools and an npm install together reach a few hundred
#   nofile     per process; Node dev tools open many files and sockets
#   tmp_size   /tmp is a tmpfs, so it's memory too
# A project's container is stopped once none of its conversations was in use for this long (services/activity),
# checked every DEVENV_IDLE_SWEEP_SECONDS. The next open (or message) starts it again.
DEVENV_IDLE_STOP_SECONDS = 2 * 60 * 60
DEVENV_IDLE_SWEEP_SECONDS = 5 * 60
DEVENV_CONTAINER_LIMITS = {"memory": "3g", "cpus": "1.0", "pids": 2048, "nofile": 65536, "tmp_size": "256m"}


class ProjectStage(StrEnum):
    """projects.stage -- where a project is in the building process. Moves forward only (see
    migrations/0005_projects.sql). The starting form before it is the FE's alone: a project only exists
    once that's submitted."""

    SPECING = "specing"  # chatting with the agent to work out the spec; no app yet
    IMPLEMENTING = "implementing"  # the app is set up (or being set up): there's something to preview


# Whether user messages are answered by the stand-in agent (services/agent/mock) instead of being sent to
# the project's real agent session. Flip it off once sending to the session is built.
AGENT_MOCK_REPLIES = False

# The ai_conversations.meta key marking that the agent was handed the project's starting form
# (services/agent/sessions.ensure_introduced) -- set by whichever open gets there first. The FE reads it too.
CONVERSATION_INTRODUCED_KEY = "introduced_at"
# How long after the introduction the agent may stay silent before it counts as failed: its first turn
# died after getting the introduction (an API error, a crash), and nothing else would ever resend it. The
# next open then introduces it again (ensure_introduced). Generous: a first turn normally takes a minute or two.
# The FE uses the same figure to offer a retry -- keep them in step (biveit-frontend INTRODUCTION_OVERDUE_MS).
INTRODUCTION_OVERDUE_SECONDS = 300

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
# The tool the agent asks the user to DO something in the UI with: input {"actions": [{name, params}]}, each
# name an AgentAction. The answer comes back like ask_user's: a tool_result with one result per action.
REQUEST_ACTIONS_TOOL_NAME = "request_actions"
# Tools whose "execution" is the user answering. A tool_use of one of these pauses the conversation
# until a matching tool_result arrives (services/agent/interactions).
USER_FACING_TOOLS = [ASK_USER_TOOL_NAME, REQUEST_ACTIONS_TOOL_NAME]


# A signal: a timestamped marker in the conversation's log ({type: SIGNAL_BLOCK_TYPE, slug, description?,
# meta?}, alone in an ASSISTANT message). Not a chat message -- the FE reads whatever state it needs off
# the log's tail (e.g. "building": the latest BUILDING with no BUILDING_DONE after it, less than an hour
# old). Platform-only: an LLM API replay of the conversation must drop these blocks.
SIGNAL_BLOCK_TYPE = "signal"


# How many of a conversation's newest messages are read to work out its current signals (e.g. whether a
# build is open). Signals that matter sit near the end of the log, so a short tail is enough.
SIGNAL_TAIL_SIZE = 50


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
