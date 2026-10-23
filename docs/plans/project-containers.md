# Plan: per-project dev containers

Status: agreed in discussion (2026-10-21), not started. Each numbered commit is reviewed and committed on its own.
Commits 4 and 5 are DEPLOYED together (see commit 4).

## The design in one page

- **One container per project**, named after it (`biveit-<env>-project-<id>`), nothing stored. Created on first use (the
  agent's session starts in it), stopped on project close / idle, removed on project delete. Disposable: all project
  state lives in its mounted folder, every option is derived from the project.
- **The user is the security boundary.** A user's projects reaching each other is fine; another user's is not.
- **Image**: one common image (`python:3.12-slim` + Node 24 (LTS), uv, pm2, Redis, git, Chromium and its libraries), versioned
  tag on Docker Hub (private repository). No secrets, no project or runner code baked in. A project records the tag it
  was created with (`projects.meta.devenv_image`); new projects get the current one, existing ones keep theirs (manual
  upgrade).
- **Mounts** (fixed container paths):

  | Host | Container | Mode |
  |---|---|---|
  | `<STORAGE_DIR>/<user hash>/<project hash>` | `/project` | rw |
  | `<storage root's parent>/agent2_sockets/<project id>` (short path: unix socket ~108-byte cap) | `/sockets` | rw |

  No backend socket mount, on purpose: calls INTO the backend (the runner's, and Claude Code's model calls -- which
  can't use a unix socket anyway) go over TCP to the agent listener on the bridge gateway. The only socket is the
  runner's own inbox, which the backend connects to.

- **Runs inside, all under pm2**: the dev servers (`project_<id>_be`, `project_<id>_fe`), the project's own Redis
  (`project_<id>_redis`, 127.0.0.1 only, data in `/project/.devenv/redis`), and one session runner per conversation
  (`runner_<conv>`). Container state: `HOME=/project/.devenv/home` (Claude Code's settings and transcripts),
  `PM2_HOME=/project/.devenv/pm2`.
- **Session runner**: its own repo, `biveit-session-runner` (with the skills/MCP bundle). `docker cp`'d into each container
  at `/opt/session-runner`, root-owned (the agent can read but not change it), own venv (`uv sync --frozen`), version
  (git commit) checked and reinstalled at every container start -- always ensured.
- **Network**: own bridge `biveit-projects`, subnet `10.213.0.0/16` (setting, default), gateway `10.213.0.1`. Containers
  reach the host only at the gateway: Mongo and the backend's agent listener. Dev ports published on the host's
  `127.0.0.1` only (host side 8000+id / 16000+id; fixed internal ports 8000 / 5173). Server firewall: no other host
  ports, no private ranges.
- **Backend <-> runner**: backend -> runner over the runner's unix socket (`/sockets/<conv>.sock`); runner -> backend
  and Claude Code -> model over TCP to the backend's **agent listener** on `10.213.0.1:3002`.
- **Model key never enters a container**: Claude Code's `ANTHROPIC_BASE_URL` = the listener's model proxy, auth = its
  session token; the backend swaps in the real key. Path allowlist: `v1/messages`, `v1/messages/count_tokens`.
- **Runs as the backend's own uid** (chosen over per-user uids). So every host-side touch of a project folder goes
  through the safe-path helpers (commit 1) -- see "PROJECT FOLDERS ARE UNTRUSTED INPUT" in CLAUDE.md.
- **Hardening**: `--cap-drop=ALL`, `no-new-privileges`, default seccomp, `--init`; never `--privileged`, never the docker
  socket.
- **Limits** (ceilings, not reservations): memory 3g (no swap), cpus 1.0, pids 2048 (counts THREADS: Chromium, Node
  and npm installs together reach a few hundred), nofile 65536 (per process; Node dev tools open many files/sockets),
  `/tmp` tmpfs 256m.
  Idle stop after 2 hours. No cap on the number of running containers for now.
- **Skills/MCPs**: platform-owned bundle, same for every project; users can't add their own. Playwright MCP, Context7
  (hosted, keyless), frontend-design and ui-ux-pro-max skills.

## Commit 1 -- safe access to project folders

No container needed; closes the symlink holes before the container makes them matter.

- `src/utils/paths.py` (new, generic):
  - `open_within(root, relative_path, flags, mode=0o600) -> int` -- walks `relative_path` under `root` one component at
    a time (`os.open(part, O_NOFOLLOW | O_DIRECTORY, dir_fd=...)`, the last with `O_NOFOLLOW`): no symlink followed
    anywhere, no check-then-open race. Raises `PathOutsideRoot` for absolute paths, `..`, or a symlink on the way.
  - `make_dirs_within(root, relative_dir)` -- same walk, creating missing folders (`os.mkdir(..., dir_fd=...)`).
  - `copy_tree_within(source_dir, root, relative_dest, skip_existing, ignored_names)` -- copies a trusted source tree
    in through the above (replaces `shutil.copytree` for seeding and the boilerplate).
- `services/devenv/workspaces.py`: project-aware wrappers rooted at `get_project_dir(project)` (its parents are the
  backend's, so the walk starts from trusted ground), e.g. `open_in_project(project, relative_path, flags)`.
- Switched over:

  | Where | Today | Now |
  |---|---|---|
  | `storage.resolve_conversation_file` + `FileResponse` | `resolve()` check (bypassed by a symlinked `artifacts/`) | open via the walk, `fstat`, stream from the fd |
  | `storage.save_upload` | `Path.open("xb")` | walk, `O_WRONLY \| O_CREAT \| O_EXCL` |
  | `storage.write_artifact` | `write_bytes` | walk, `O_CREAT \| O_TRUNC` |
  | `storage.list_files` | listing | `os.scandir` on the walked fd, symlinks skipped |
  | `environments._write_private_file` (env files) | temp + `os.replace` | temp through the walk, `os.rename(src_dir_fd=, dst_dir_fd=)` in the verified folder |
  | `servers._write_ecosystem_file`, `logs/` | `open(path, "w")`, `makedirs` | through the walk |
  | `workspaces._copy_missing_files`, `copy_boilerplate` | `shutil.copytree` | `copy_tree_within` |
  | `adapter` writing `.mcp.json` (agent1) | `open(..., "w")` | through the walk |
  | `scripts/move_projects_to_owner_mongo.py`, `project_status` reads | `open` | through the walk |

- Tests (`tests/unit/test_paths.py` + per call site): planted symlinks (file, folder, relative `../../..`) are refused
  and their targets untouched; normal files work; `..` and absolute paths refused.

## Commit 2 -- image and container lifecycle

The container exists but runs nothing yet.

- `data/devenv_image/Dockerfile`: `python:3.12-slim`, Node 24 (LTS) (copied from the official image), uv, pm2, `redis-server`,
  git, procps, Chromium + its libraries (`npx playwright install --with-deps chromium`, as root). Build context is that
  folder only; no secrets, no build args carrying any.
- `scripts/build_devenv_image.sh <tag>`: build + push to Docker Hub (private repository).
- Constants: `DEVENV_IMAGE` (current tag), `DEVENV_NETWORK = "biveit-projects"`, `DEVENV_PROJECT_PATH = "/project"`,
  `DEVENV_SOCKETS_PATH = "/sockets"`, `DEVENV_LIMITS` (memory 3g, cpus 1.0, pids 2048, nofile 65536, tmp 256m).
  Setting: `DEVENV_NETWORK_SUBNET` (default `10.213.0.0/16`; gateway = first address).
- `services/devenv/containers.py` (new):
  - `get_container_name(project_id)`, `get_host_socket_dir(project_id)`, `to_container_path(project, host_path)`
    (raises if the path isn't under the project folder), `get_gateway_ip()`.
  - `ensure_container(project)`, branching on `docker inspect`:
    - no container -> record `meta.devenv_image = DEVENV_IMAGE` if the project has none, ensure the network (fixed
      subnet, bridge interface named after it) and the socket folder (0700), `docker run`;
    - exists, stopped -> `docker start`;
    - running -> nothing;
    - lost a creation race (name taken) -> re-inspect, fine if running;
    - anything else -> `ContainerUnavailable`.
    The image always comes from `project.meta.devenv_image` once recorded.
    Every start also runs `ensure_container_user` (`docker exec -u root useradd -u <uid>` if missing: some tools look
    the user up by name).
  - `_to_run_argv(project)`: the two mounts, `--user <backend uid>:<gid>`, `HOME`/`PM2_HOME`, published
    `127.0.0.1:<host port>:<internal port>`, the limits (+ `--memory-swap` = memory), `--cap-drop=ALL`,
    `--security-opt=no-new-privileges`, `--init`, command `sleep infinity`.
  - `stop_container(project_id)`, `remove_container(project_id)`, `is_container_running(project_id)`,
    `run_in_container(project_id, argv, cwd=None, user=None, env=None, timeout)` (env by NAME: values from docker's own
    env, never on the command line), `copy_into_container(project_id, host_path, container_path)`.
- Hooks: `projects.close_project` -> `stop_container` last; `delete_project` -> `remove_container`.
- `scripts/project_status.py`: the container, its image tag, its state.
- Tests: the `docker run` options (only the two mounts, ports on 127.0.0.1, no `--privileged` / docker socket, hardening
  flags present); each `ensure_container` branch (docker faked); a recorded tag wins over the constant.
- Verify for real: build the image; create / start / stop / remove a test project's container.

## Commit 3 -- dev servers and Redis in the container

- `biveit-boilerplate`: a `setup.sh` (its own small commit there). Contract: idempotent and quick when nothing changed
  (`uv sync` in `backend/`, `npm install` in `frontend/` when needed); non-zero exit + stderr on failure; an
  interrupted install never looks finished (what the backend's npm-lockfile check does today moves in here).
- `environments.ensure_devenv` order: workspace -> boilerplate (host, `copy_tree_within`) -> Mongo access -> env files
  (host, through the walk) -> `ensure_container` -> `./setup.sh` inside it (falls back to the current `uv sync` /
  `npm install` in the container when the workspace has no script) -> pm2 inside -> nginx site -> health check.
- `servers.py`: every `pm2` call through `run_in_container`; a container that isn't running = no dev servers
  (`missing`).
- Ecosystem template: a third app, `project_<id>_redis` (`redis-server --bind 127.0.0.1 --port 6379 --dir
  /project/.devenv/redis --maxmemory 128mb --maxmemory-policy allkeys-lru`, default snapshots, no AOF).
- Templates: fixed internal ports (`DEVENV_INTERNAL_BACKEND_PORT = 8000`, `DEVENV_INTERNAL_FRONTEND_PORT = 5173`),
  `0.0.0.0`; `REDIS_URL=redis://127.0.0.1:6379/0`; Mongo via `PROJECT_MONGO_HOST` (`10.213.0.1:27017` on the server);
  `VITE_API_PROXY_TARGET=http://127.0.0.1:8000`. nginx and health checks keep the host ports.
- `describe_devenv` / "[Dev environment ready]": internal ports + the site URL, and "run ./setup.sh after changing
  dependencies".
- Tests: setup order (container faked); every pm2 call goes through the container; the shipped templates render.

## Commit 4 -- the session runner as its own repo (still on the host)

DEPLOY TOGETHER WITH COMMIT 5: once dev servers are in the container, an agent on the host breaks their venv (its
`uv run` rebuilds it for the host). Commit 4 alone is a fine local state, not a deployable one.

- New repo `/home/anhlq/workspace/biveit-session-runner` (`git init`, no remote yet):
  - `pyproject.toml` (claude-agent-sdk, aiohttp) + `uv.lock`;
  - `session_runner/__main__.py` (today's `src/runners/agent2/main.py`), `session_runner/channel.py`;
  - `session_runner/protocol.py`: env var names, `AGENT_SIGNALS`, `AgentAction`, `ApprovalDecision`,
    `InteractionStatus`, `DevServerAction`, the MCP name, the permission mode -- the ONE source for both sides;
  - `tests/`: the runner half of `tests/unit/test_agent2.py`.
- Backend: `[tool.uv.sources] biveit-session-runner = { path = "../biveit-session-runner", editable = true }`;
  `src/constants.py` re-exports the protocol names from `session_runner.protocol` (existing imports keep working);
  `src/runners/` removed; setting `SESSION_RUNNER_DIR` (default `../biveit-session-runner`).

## Commit 5 -- the agent in the container

- **Agent listener** (`src/app.py` lifespan): a second uvicorn server (`startup()`/`shutdown()`, not `serve()`) for an
  agent-only app on `<gateway ip>:<AGENT_LISTENER_PORT = 3002>`: the agent routes + the model route. Network ensured
  first; no docker on the machine -> log and skip the listener.
- **Model proxy**: `services/agent2/models.py` + `controllers/agent.py`'s `socket_router` (listener only, not the public
  app). `POST /api/agent/model/v1/messages` and `.../v1/messages/count_tokens`, anything else 404. Our credentials
  replace the caller's; streamed back. No key configured -> 502; upstream unreachable -> 502; upstream error -> passed
  through unchanged.
- **Runner install** -- `services/agent2/runners.py` (new): `ensure_runner_installed(project)`: read
  `/opt/session-runner/VERSION`, compare with the host checkout's commit:
  - equal -> done;
  - different / missing -> `docker cp` the checkout (minus `.git`/`.venv`), then as root `UV_NO_CACHE=1 uv sync --frozen
    --compile-bytecode`, also installing the bundle's pinned MCP servers, write `VERSION`, `chmod -R a+rX`;
  - failure -> `RunnerUnavailable`.
  Called from `ensure_session` right after `ensure_container`, so every container start passes through it.
- **Bundle** (in the runner repo): a manifest (each MCP's name, type stdio/http, command or URL, pinned version, needs a
  key or not) + a `skills/` folder laid out as a Claude Code plugin.
  - Playwright MCP: `@playwright/mcp` pinned, headless, `--no-sandbox` (Chromium's own sandbox needs the privileges we
    drop; the container is the sandbox).
  - Context7: hosted, keyless.
  - frontend-design, ui-ux-pro-max: vendored at pinned commits, after a license and content review (ui-ux-pro-max runs
    Python scripts: the image has Python).
  - Runner options: `mcp_servers` = ours + the manifest's, `plugins` = the bundle's skills, `skills="all"`,
    `strict_mcp_config=True` (the agent can't add servers via `.mcp.json`), allow only `mcp__<declared server>`.
- **`services/agent2/sessions.py`**:
  - `ensure_session`, on the conversation's socket: answers health -> done; accepts connections but no health answer
    (hung) -> `pm2 delete runner_<conv>` then fresh start; nothing there -> start. Then wait for health (30s).
  - start: `pm2 start /opt/session-runner/.venv/bin/python --name runner_<conv> --cwd /project/<conv> --interpreter none
    --output /project/.devenv/logs/runner_<conv>.log -- -m session_runner`, env by name: `AGENT_SOCKET=/sockets/<conv>.sock`,
    `BACKEND_URL=http://<gateway>:3002`, `COMMUNICATION_TOKEN`, `DISABLE_AUTOUPDATER=1`, `PYTHONDONTWRITEBYTECODE=1`, the
    model names. Two racing starts: the runner's file lock keeps exactly one (unchanged).
  - stop: `pm2 delete runner_<conv>` (SIGTERM; graceful).
  - the backend connects to `<host socket dir>/<conv>.sock`.
- **Runner side**: calls back to `BACKEND_URL`; Claude Code gets `ANTHROPIC_BASE_URL=$BACKEND_URL/api/agent/model`,
  `ANTHROPIC_AUTH_TOKEN=<session token>`, no API key.
- Removed: `src/utils/systemd.py`, the detached-start fallback. Scripts: `project_status` lists runner processes.
- Tests: runner install's version branches; the start argv carries no secret values; the hung-runner branch; the model
  route's allowlist and its absence from the public app; the auth swap.
- Verify for real: one real turn through the proxy, a question, an approval, `dev_servers` from inside, Playwright
  opening the dev site.

## Commit 6 -- idle stop

- Activity = Redis key `devenv:active:<project_id>`, TTL 2 hours (`DEVENV_IDLE_STOP_SECONDS`), refreshed by `open_project`, a user message, the agent
  posting a message, and each SSE heartbeat of an open chat tab.
- Sweep: a loop in the app lifespan, every 5 min, under a Redis lock (one worker runs it): every running
  `biveit-<env>-project-*` container whose key is gone -> `close_project`; key present -> leave it.
- No cap on how many containers run at once, for now. If memory gets tight: a cap in `ensure_container` that closes the
  least recently active one, sized from the server's real memory (or keyed on available memory instead of a count).
- Tests: the sweep's branches (docker, Redis faked).

## Commit 7 -- migration and server setup

- `scripts/move_projects_into_containers.py [--apply]`, per live project with an app: remove its old host pm2 servers;
  stop old host runners (systemd units / detached processes); rewrite env lines (`API_HOST`, ports, `REDIS_URL`, the
  `MONGO_URI` host, the Vite proxy target) through the walk; delete `node_modules` and `.venv`; copy Claude transcripts
  from the host's `~/.claude/projects/<encoded host path>` into `.devenv/home/.claude/projects/-project-<conv>`. The next
  open rebuilds the rest.
- `scripts/devenv_firewall.sh` (root, server): INPUT from `biveit-projects` -> established, the Mongo port, the listener
  port; drop the rest. DOCKER-USER from `biveit-projects` -> drop private ranges (incl. container to container).
- `docs/devenv-server-setup.md`: Mongo listening on `10.213.0.1` with auth on; the backend in the `docker` group;
  `docker login`; `STORAGE_DIR` outside the repo; `.env` at 0600; nothing public on `0.0.0.0` but nginx and SSH
  (`sudo ss -ltnp`); file-watcher limits raised (`fs.inotify.max_user_watches=524288`,
  `fs.inotify.max_user_instances=1024` in `/etc/sysctl.d/`) -- they're per uid, and every container runs as the
  backend's uid, so all projects' Vite/uvicorn watchers share one pool.

## Follow-ups (not in this plan)

- Drop agent1; then take `/api/agent/*` off the public app (a leaked session token is then useless outside its
  container).
- Separate dev and prod derived secrets (`project:<id>:prod:<name>`): today the dev container holds the prod site's
  `JWT_SECRET` / `ADMIN_TOKEN`.
- Mongo 1GB-per-user quota (periodic `dbStats`, downgrade readWrite -> read); disk monitoring per project folder.
- Metering / caps on the model proxy; bandwidth limits; maybe an outbound allowlist.
- Runner TODOs (in its header): retries + idempotency key on posts to the backend; keeping in-memory waits in sync with
  the log; recovering from a poisoned transcript; surfacing errors to the user.
- Prod deployments on the same pattern (own Redis in container, per-project DB credentials).
