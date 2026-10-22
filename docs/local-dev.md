# Local development

How to get the whole biveit platform running on your machine: the backend (this repo), the frontend, and
the agent sessions that build users' websites.

> **macOS vs Linux.** Both work, real agent sessions included: the backend finds a conversation's
> running Claude session through psutil, which reads processes on either. One macOS quirk: it hides the
> environment of Apple's own binaries (`/bin/sleep`, `/bin/sh`...) from other processes, so a session
> must be a non-system binary -- `claude` is, and the adapter tests use Python stand-ins for that reason.

## What runs where

```
browser ── biveit-frontend (Vite, :5173)
              │  /api
              ▼
           biveit-backend (FastAPI, :3001) ── Postgres, Redis
              │  starts, in tmux, one per conversation
              ▼
           claude  ── its communication MCP (src/mcps/communication) ── calls back into :3001
              │  once the agent starts implementing
              ▼
           the user's project: backend :10101 + frontend :10102 (pm2), with its own Mongo db + Redis db
```

The agent sessions and the projects' dev servers run **directly on your machine**, not in containers.
Only the databases run in Docker.

## 1. Install the tools

| Tool | Why | Install |
|---|---|---|
| `claude` (logged in) | the agent | you have it already. Check `claude --version` works in a new terminal |
| Python 3.12 + [uv](https://docs.astral.sh/uv/) | the backend | `curl -LsSf https://astral.sh/uv/install.sh \| sh` (uv fetches Python itself) |
| Node.js ≥ 22 + npm | the frontend, and the projects the agent builds | [nvm](https://github.com/nvm-sh/nvm): `nvm install 22` |
| pm2 | runs the projects' dev servers | `npm install -g pm2` |
| tmux | runs the agent sessions | Linux: `sudo apt install tmux` · macOS: `brew install tmux` |
| Docker | Postgres, Redis, Mongo | Docker Desktop, or Docker Engine on Linux |

The backend runs `claude`, `tmux`, `pm2`, `uv` and `npm` itself, so they must be on the `PATH` of
the terminal you start the backend from. If you installed Node with nvm, open a fresh terminal (or
`nvm use 22`) before starting it.

## 2. Clone the repos side by side

```bash
mkdir -p ~/workspace && cd ~/workspace
git clone git@github.com:tna20141/biveit-backend.git
git clone git@github.com:tna20141/biveit-frontend.git
git clone <biveit-boilerplate repo URL> biveit-boilerplate
```

The backend expects the boilerplate (the app copied into each project once the agent starts
implementing) at `../biveit-boilerplate`. If you put it elsewhere, set `BOILERPLATE_DIR` in `.env.local`.

## 3. Start the databases

```bash
docker run -d --name biveit-postgres --restart unless-stopped \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=biveit \
  -p 127.0.0.1:5432:5432 postgres:16

docker run -d --name biveit-redis --restart unless-stopped \
  -p 127.0.0.1:6379:6379 redis:7 redis-server --databases 18000

docker run -d --name biveit-mongo --restart unless-stopped \
  -p 127.0.0.1:27017:27017 mongo:7
```

- **Postgres** and **Redis** (db 0) are the platform's own.
- **Mongo** and the rest of Redis are what the users' generated apps use: each project gets its own Redis
  database (dev: db `<project id>`, prod: db `8000 + <project id>`). Redis only has 16 by default, hence
  `--databases 18000`. Using a Redis you already run? Set `databases 18000` in its config (e.g.
  `/etc/redis/redis.conf`) and restart it, or projects with an id above 15 can't use Redis.

These match the defaults in `.env.example`. If you already run any of them locally on those ports,
skip that container and point the env file at yours.

## 4. Set up the backend

```bash
cd ~/workspace/biveit-backend
uv sync
cp .env.example .env.local
cp .env.migration.example .env.migration
```

Fill in these secrets in `.env.local`. Any long random string works; generate each one with
`openssl rand -hex 32`. They're required: with any of them empty, things quietly fail closed.

| Key | Without it |
|---|---|
| `JWT_SECRET` | nobody can log in |
| `SESSION_TOKEN_SECRET` | no agent session can start |
| `PROJECT_SECRET_SEED` | the agent can't set up a project's app |
| `ADMIN_TOKEN` | the admin endpoints are off (optional) |

Leave `DEV_DOMAIN` empty: locally there's no nginx, and every project's dev site opens straight on
`http://localhost:10102`. Google login is optional (`GOOGLE_CLIENT_ID` and friends); email/password
works without it.

Create the tables, then start the backend:

```bash
uv run python -m scripts.migrate
uv run python main.py          # http://localhost:3001, reloads on save
```

Check it: `curl localhost:3001/api/debug/health` should answer `{"ok": true}`.

## 5. Set up the frontend

```bash
cd ~/workspace/biveit-frontend
npm install
npm run dev                    # http://localhost:5173
```

Its committed `.env` already points at `http://localhost:3001/api`.

## 6. Try it

1. Open http://localhost:5173 and register.
2. Create a project and fill in the starting form. The page waits a few seconds for the agent to come
   up, then the agent opens the chat with a welcome and a first question.
3. Chat until the plan is agreed. The agent then asks you to approve the spec (Approve / Keep
   iterating buttons). Approving copies the boilerplate into the project, installs its dependencies and
   starts its dev servers. The preview then appears next to the chat.

The first time a session starts in a new folder, Claude shows two startup screens: the folder-trust
dialog and the development-channels warning. The backend answers both on its own.

**Watching an agent:** each conversation's session is a tmux session named `conv_<conversation id>`.

```bash
tmux ls                        # list them
tmux attach -t conv_9          # watch one (detach with Ctrl-b d, never Ctrl-c)
```

**Where things are on disk:** `data/storage_root/<user hash>/<project hash>/<conversation id>/` is a
project's workspace: the agent's files, the app (`backend/`, `frontend/`) once implementing, the chat's
`uploads/` and `artifacts/`, and the dev servers' `logs/`.

## Useful commands

```bash
uv run python -m scripts.project_status            # the whole platform: config, infra, what's running
uv run python -m scripts.project_status <id>       # one project: workspace, session, dev servers, data
uv run python -m scripts.stop_project <id>         # turn a project's session and dev servers off
uv run pytest tests                                # the test suite
uv run ruff check --fix && uv run ruff format      # lint + format
```

Opening the project again in the browser brings back whatever `stop_project` turned off. The agent's
session continues its previous conversation.

## Mock mode (no real agent)

Set `AGENT_MOCK_REPLIES = True` in `src/constants.py` and restart the backend. No Claude sessions are
started; a stand-in agent answers in the chat instead:
- `text`: echoes your message;
- `question` / `questions`: multiple-choice questions;
- `artifact`: writes and shows a file;
- an attachment: lists the uploads.

It's for working on the chat UI without spending model turns.

## Things to know

- **One project's dev servers at a time.** Locally every project uses the same two ports (10101 and
  10102). Stop the previous project first (`scripts.stop_project <id>`), or the next one's setup fails
  with the ports taken.
- **The agent's tools come from this repo.** `src/mcps/communication` is the MCP server every session
  runs. Each session's `.mcp.json` is written when the session starts, pointing at this repo's Python.
  So after changing the MCP, restart the session (`stop_project`, then open the project) to pick it up.
- **Template changes only reach new workspaces.** `data/template/` (`CLAUDE.md`, `.claude/`) is copied
  into a workspace only where files are missing. Existing projects keep their copy.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Opening a project fails with 503 | the session couldn't start. Check `tmux ls` and the `<conversation id>.session.log` next to the workspace. `claude` or `tmux` not on the backend's `PATH` is the usual cause |
| The agent never replies | attach to its tmux session and look: a startup screen nobody answered, or an error. `scripts.project_status <id>` shows whether the channel port is listening |
| A session sits on "New MCP server found" | the workspace lacks `.claude/settings.local.json` (`enableAllProjectMcpServers`). It comes from `data/template/` and is copied only where missing: copy it in, then restart the session (`scripts.stop_project <id>`, then open the project) |
| The preview never comes up | `scripts.project_status <id>` shows the dev servers. Their logs are in the workspace's `logs/`. Ports 10101/10102 held by another project is common |
| Login always 401s | `JWT_SECRET` is empty |
| `Too many sessions` | two Claude processes in one workspace (e.g. one started by hand). `scripts.stop_project <id>` clears them |
