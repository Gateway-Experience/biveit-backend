# Setting up a server (Ubuntu)

A fresh Ubuntu Server 24.04 box running the whole platform: the backend, the frontend, the databases,
and one container per project. Everything runs as the `ubuntu` user (home `/home/ubuntu`). Below,
`biveit.example.com` is the platform and `*.dev.biveit.example.com` the projects' dev sites -- replace both
with your domain.

## What runs where

| Thing | Where | Listens on |
|---|---|---|
| nginx: the platform's site, the frontend's build, every project's dev site | host | `0.0.0.0:80/443` (the only public ports, with SSH) |
| backend API | host, systemd `biveit-backend` | `127.0.0.1:3001` (nginx proxies `/api/`) |
| agent listener: the agent endpoints + the model proxy (same process as the API) | host | `10.213.0.1:3002` (the containers' gateway only) |
| Postgres, Redis (the platform's) | Docker, network `biveit-db` | published on `127.0.0.1` only |
| Mongo (the projects' apps' data) | Docker, network `biveit-db` + `biveit-projects` at `10.213.0.2` | published on `127.0.0.1` for the backend; `10.213.0.2:27017` for the projects (with auth) |
| one container per project: its dev servers, its own Redis, its agents' session runners, Claude Code | Docker, network `biveit-projects` (10.213.0.0/24) | dev ports published on `127.0.0.1` only: backend 8000 + id, frontend 16000 + id |

The rules this layout keeps:
- A project container sees its own folder (`/project`) and its runner sockets (`/sockets`). Nothing
  else from the host is mounted, and it never gets the Docker socket or any capability.
- From inside a project container, exactly **two private addresses** answer: the agent listener
  (`10.213.0.1:3002`, on the host) and Mongo (`10.213.0.2:27017`). Everything else on the host, other
  containers and private networks (including cloud metadata) are blocked. The public internet is open
  (npm, PyPI...).
- **The model key never enters a container.** Claude Code calls the model through the backend's proxy,
  authenticated with its conversation's session token, and the backend adds the real key.
- Containers run as `ubuntu`'s uid. So host-side code must never follow a path inside a project folder
  (see "PROJECT FOLDERS ARE UNTRUSTED INPUT" in CLAUDE.md).

> **The `ubuntu` account is effectively root.** It's in the `docker` group, which is root-equivalent
> (and on cloud images it also has passwordless `sudo`). A compromise of the backend is a compromise of
> the server: guard its SSH access and its `.env.prod` accordingly.

## 0. Before you start

You'll need:
- **The server.** Each project's container may use up to 3 GB of memory and 1 CPU (limits, not
  reservations), and a container stops after 2 hours without activity. Size the RAM for how many projects
  are actively being worked on at once, plus ~2 GB for the platform. Disk: every project holds its own
  `node_modules` and `.venv` (several hundred MB each), so give it room (100 GB+).
- **DNS:** two A records to the server -- `biveit.example.com` and `*.dev.biveit.example.com` -- plus an API
  token for your DNS provider (certbot needs it for the wildcard certificate).
- **Read access to the four repos** (`Gateway-Experience/biveit-backend`, `biveit-frontend`,
  `biveit-boilerplate`, `biveit-session-runner`).
- **A model key:** an Anthropic API key, or another Anthropic-compatible endpoint and its key (e.g.
  DeepSeek).
- **Optional:** a Docker Hub account with access to the dev image's private repository (step 9), Google
  OAuth credentials (Google login), and AWS SES (password-reset emails).

Check the projects' subnet doesn't collide with anything the server already uses (a VPC, a VPN):
`ip -4 route` should show nothing in `10.213.0.0/24`. If it does, pick another /24 and use it everywhere
below as well as in `DEVENV_NETWORK_SUBNET` (step 8). **Don't change it once projects exist:** it's
written into their env files.

## 1. Base system

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y unattended-upgrades && sudo dpkg-reconfigure -plow unattended-upgrades
sudo timedatectl set-timezone UTC
```

SSH: keys only, no root login. In `/etc/ssh/sshd_config.d/90-biveit.conf`:

```
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
```

Then `sudo systemctl reload ssh`. Check from a second terminal that you can still log in **before**
closing the first one.

## 2. Packages

```bash
sudo apt install -y git nginx certbot python3-certbot-dns-<provider> ufw curl ca-certificates
```

`<provider>` is your DNS host's certbot plugin: `cloudflare`, `digitalocean`, `route53`, etc.

**Docker**, from Docker's own repository (Ubuntu's `docker.io` works too, but lacks buildx for the image
build):

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
sudo usermod -aG docker ubuntu
```

Log out and back in, so the `docker` group applies. Then, as `ubuntu`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh                                    # uv: the backend
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash    # nvm
source ~/.bashrc && nvm install 24                                                 # node: builds the frontend
```

The host needs no Claude Code, tmux or pm2. All of that runs inside the projects' containers.

## 3. Docker settings and the projects' network

`/etc/docker/daemon.json`:

```json
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" },
  "dns": ["1.1.1.1", "8.8.8.8"]
}
```

- **`log-opts`:** caps every container's Docker log.
- **`dns`:** containers resolve names through public resolvers. The firewall (step 4) blocks private
  ranges from containers, and many clouds' resolvers live in one (AWS's `10.x.0.2`, GCP's
  `169.254.169.254`). Without this, containers couldn't resolve anything there.

```bash
sudo systemctl restart docker
```

Two networks:
- **`biveit-db`**, the databases' own. Name it whatever your servers use for the app's infra containers.
- **`biveit-projects`**, the projects' containers'. Mongo joins it as well (step 6), at a fixed address
  (`10.213.0.2`).

```bash
docker network create biveit-db
docker network create --subnet=10.213.0.0/24 --ip-range=10.213.0.128/25 \
  --opt com.docker.network.bridge.name=biveit-projects biveit-projects
ip -4 addr show biveit-projects        # inet 10.213.0.1/24: the gateway, i.e. the host
```

- **`--ip-range`:** the projects' containers get addresses from `.128` up only, so nothing ever takes
  Mongo's `.2`. That leaves room for ~125 running project containers.
- **Create `biveit-projects` by hand, with exactly this command.** The backend only creates it when it's
  missing, and without `--ip-range`. If it ever needs recreating, use this command again, then reconnect
  Mongo (step 6).

Docker recreates both networks' interfaces at every boot, and reconnects their containers.

## 4. Firewall

Two layers:
- **ufw** handles what reaches the host. That includes traffic from the containers, which ufw sees
  coming in on the `biveit-projects` interface.
- **Docker's `DOCKER-USER` chain** handles what containers reach *through* the host: other containers
  (Mongo included), private networks.

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
# From the projects' containers: the agent listener, nothing else on the host
sudo ufw allow in on biveit-projects to 10.213.0.1 port 3002 proto tcp
```

Append this to the end of `/etc/ufw/after.rules`, so it's loaded with ufw at every boot:

```
# biveit: what the projects' containers may reach THROUGH the host (docs/dev-server-guide.md). Mongo's
# port, and the public internet. Other containers (same bridge), private ranges, link-local (cloud
# metadata) and CGNAT are dropped. Replies to connections already allowed pass first.
*filter
:DOCKER-USER - [0:0]
-A DOCKER-USER -i biveit-projects -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
-A DOCKER-USER -i biveit-projects -d 10.213.0.2 -p tcp --dport 27017 -j RETURN
-A DOCKER-USER -i biveit-projects -d 10.0.0.0/8 -j DROP
-A DOCKER-USER -i biveit-projects -d 172.16.0.0/12 -j DROP
-A DOCKER-USER -i biveit-projects -d 192.168.0.0/16 -j DROP
-A DOCKER-USER -i biveit-projects -d 169.254.0.0/16 -j DROP
-A DOCKER-USER -i biveit-projects -d 100.64.0.0/10 -j DROP
-A DOCKER-USER -j RETURN
COMMIT
```

```bash
sudo ufw enable
sudo systemctl restart docker          # re-links DOCKER-USER into Docker's chains
sudo iptables -S DOCKER-USER           # the rules above
```

Every published Docker port in this guide is bound to `127.0.0.1`. That matters: Docker publishes ports
around ufw, so a port published on `0.0.0.0` would be public whatever ufw says. Never publish one that
way.

Step 15 checks all of this from inside a real container.

## 5. Kernel settings

File-watcher limits are counted per uid. Every container runs as `ubuntu`, so all projects' Vite and
uvicorn watchers share one pool, and the defaults run out after a few projects.

`/etc/sysctl.d/90-biveit.conf`:

```
fs.inotify.max_user_watches = 524288
fs.inotify.max_user_instances = 1024
```

```bash
sudo sysctl --system
```

## 6. Databases

Generate every password and secret in this guide with `openssl rand -hex 32`. Hex keeps them safe to
paste into URLs.

Each database is its own container on `biveit-db`. Each publishes its port on `127.0.0.1` only, which is
how the backend (on the host) reaches it.

```bash
docker run -d --name biveit-postgres --restart unless-stopped --network biveit-db -p 127.0.0.1:5432:5432 \
  -v biveit-postgres:/var/lib/postgresql/data \
  -e POSTGRES_PASSWORD=<pg password> -e POSTGRES_DB=biveit postgres:16

docker run -d --name biveit-redis --restart unless-stopped --network biveit-db -p 127.0.0.1:6379:6379 \
  -v biveit-redis:/data redis:7 redis-server --requirepass <redis password> --appendonly yes
```

**Mongo** holds the projects' apps' data, and the projects' code connects to it from inside their
containers. So it also joins `biveit-projects`, at the fixed `10.213.0.2`, and the firewall (step 4) lets
the projects reach that one port of it.

```bash
docker run -d --name biveit-mongo --restart unless-stopped --network biveit-db -p 127.0.0.1:27017:27017 \
  -v biveit-mongo:/data/db \
  -e MONGO_INITDB_ROOT_USERNAME=root -e MONGO_INITDB_ROOT_PASSWORD=<mongo root password> \
  mongo:7
docker network connect --ip 10.213.0.2 biveit-projects biveit-mongo
```

- **Auth** is on as soon as a root user is set (`MONGO_INITDB_ROOT_*`). It has to be: the projects'
  code, which the agents write, can reach this server.
- **Give the projects a Mongo of their own.** Don't point them at one that other apps on the server use:
  per-user logins are the only wall between their data and everything else on it.

The backend's own Mongo login only manages users. It creates one Mongo user per project owner and grants
it each of their projects' dev databases, so it needs `userAdminAnyDatabase`, not root:

```bash
docker exec -it biveit-mongo mongosh -u root -p <mongo root password> --authenticationDatabase admin --eval '
  db.getSiblingDB("admin").createUser({user: "biveit", pwd: "<mongo biveit password>",
    roles: [{role: "userAdminAnyDatabase", db: "admin"}]})'
```

Keep the root password somewhere safe, outside the server. Nothing on the server needs it.

## 7. Code

The four repos sit side by side under `~/workspace`. The backend finds the boilerplate and the session
runner as siblings (`../biveit-boilerplate`, `../biveit-session-runner`). The runner is also one of the
backend's Python dependencies.

For read-only access, give each repo a GitHub **deploy key** (GitHub allows a key on one repo only), and
an SSH host alias per key in `~/.ssh/config`:

```
Host gh-biveit-backend
    HostName github.com
    User git
    IdentityFile ~/.ssh/deploy_biveit-backend
    IdentitiesOnly yes
# ...the same for biveit-frontend, biveit-boilerplate, biveit-session-runner
```

```bash
for repo in biveit-backend biveit-frontend biveit-boilerplate biveit-session-runner; do
  ssh-keygen -t ed25519 -N "" -f ~/.ssh/deploy_$repo -C "$repo@$(hostname)"   # add each .pub as a deploy key
done
mkdir -p ~/workspace && cd ~/workspace
for repo in biveit-backend biveit-frontend biveit-boilerplate biveit-session-runner; do
  git clone gh-$repo:Gateway-Experience/$repo.git
done
cd biveit-backend && uv sync
```

## 8. Configuration

The data lives outside the repos:

```bash
mkdir -p ~/data/storage_root
chmod 750 ~ ~/data           # 750 is Ubuntu's default for homes; nobody else reads in here
```

The runners' sockets go next to it, in `~/data/agent2_sockets/` (created on demand). A unix socket's
path is capped at ~108 bytes, so keep `~/data` short.

`cd ~/workspace/biveit-backend && cp .env.example .env.prod && chmod 600 .env.prod`. Then set (anything
not listed keeps `.env.example`'s value):

```
ENV=prod
API_RELOAD=false
POSTGRES_DSN=postgresql://postgres:<pg password>@localhost:5432/biveit
REDIS_URL=redis://:<redis password>@localhost:6379/0

# Each one: openssl rand -hex 32. Different values, never reused across servers.
JWT_SECRET=
SESSION_TOKEN_SECRET=
PROJECT_SECRET_SEED=
ADMIN_TOKEN=

# The model, ONE of:
ANTHROPIC_API_KEY=
#   or another Anthropic-compatible endpoint:
# ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
# ANTHROPIC_AUTH_TOKEN=

FRONTEND_URL=https://biveit.example.com
STORAGE_DIR=/home/ubuntu/data/storage_root
DEV_DOMAIN=dev.biveit.example.com
PROJECT_MONGO_URI=mongodb://biveit:<mongo biveit password>@127.0.0.1:27017/?authSource=admin
PROJECT_MONGO_HOST=10.213.0.2:27017            # Mongo on the projects' network (step 6)
DEVENV_NETWORK_SUBNET=10.213.0.0/24

# Google login (optional): all three, or none
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
SESSION_SECRET=
GOOGLE_REDIRECT_URI=https://biveit.example.com/api/auth/google/callback

# Password-reset emails through SES (optional)
EMAIL_FROM=
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
```

- **`MASTER_PASSWORD` stays empty.** It logs in as anyone.
- **`PROJECT_SECRET_SEED`** derives every project's secrets and its owner's Mongo password. Changing it
  later breaks existing projects' env files and Mongo logins, so set it once and back it up.
- **`PROJECT_REDIS_URL`** is a leftover: projects now run their own Redis in their container. Leave it
  unset.
- `ENV=prod` names this platform's containers `biveit-prod-project-<id>`. Any label works, but keep it
  fixed.

Migrations read their own file:

```bash
cp .env.migration.example .env.migration && chmod 600 .env.migration
# set POSTGRES_DSN_PROD to the same DSN as above
uv run python -m scripts.migrate --target prod --confirm
```

## 9. The projects' image

Every project container runs one common image, named by `DEVENV_IMAGE` in `src/constants.py`. It's
made from `data/devenv_image/Dockerfile`: Python 3.12, Node 24, uv, pm2, Redis, git, and Chromium for
the agents' Playwright MCP.

**Build it on the server** (one server, no registry needed):

```bash
cd ~/workspace/biveit-backend && scripts/build_devenv_image.sh
```

**Or pull it from Docker Hub.** That needs `DEVENV_IMAGE` to name the Docker Hub repository
(`<namespace>/biveit-devenv:<n>`). Today it's a bare `biveit-devenv:1`, which only works when built locally.

```bash
docker login                      # stores the login in ~/.docker/config.json, which the backend uses too
docker pull <namespace>/biveit-devenv:<n>
```

Have the image on the server before anyone opens a project. Otherwise the first project's open waits for
the pull.

## 10. Certificates

```bash
# The DNS plugin's credentials (format: the plugin's docs), readable by root only
sudo install -d -m 700 /root/.secrets
sudo install -m 600 /dev/null /root/.secrets/certbot-<provider>.ini && sudo nano /root/.secrets/certbot-<provider>.ini

# Wildcard for the dev sites. The backend's nginx template expects it at /etc/letsencrypt/live/dev.biveit.example.com/
sudo certbot certonly --dns-<provider> --dns-<provider>-credentials /root/.secrets/certbot-<provider>.ini \
  -d dev.biveit.example.com -d '*.dev.biveit.example.com'
```

The platform's own certificate comes after its nginx site (step 11). certbot installs a renewal timer for
both. `sudo certbot renew --dry-run` checks it works.

## 11. nginx

```bash
sudo rm /etc/nginx/sites-enabled/default
```

In `/etc/nginx/conf.d/00-biveit-hardening.conf`:

```nginx
server_tokens off;
```

**The platform's site**, at `/etc/nginx/sites-available/biveit`:

```nginx
server {
    listen 80;
    server_name biveit.example.com;
    root /var/www/biveit;                       # the frontend's build (step 12)

    location /api/ {
        proxy_pass http://127.0.0.1:3001;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;   # overwrite, never append
        proxy_read_timeout 1h;                # the chat's SSE stream stays open
        client_max_body_size 30m;             # uploads are up to 25 MB a file
    }

    location / {
        try_files $uri /index.html;           # SPA routes
    }
}
```

```bash
sudo mkdir -p /var/www/biveit
sudo ln -s /etc/nginx/sites-available/biveit /etc/nginx/sites-enabled/biveit
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d biveit.example.com        # adds the 443 server and the redirect
```

**Projects' dev sites:** the backend writes one file per project into `/etc/nginx/conf.d/`
(`biveit-project-<id>.conf`, from `data/devenv_templates/nginx-dev.conf.tmpl`) and reloads nginx. It
does that with `sudo -n`.

On a cloud image, `ubuntu` already has passwordless `sudo`, so there's nothing to do. Elsewhere, allow
exactly those commands with `sudo visudo -f /etc/sudoers.d/biveit`:

```
ubuntu ALL=(root) NOPASSWD: /usr/bin/tee /etc/nginx/conf.d/biveit-project-*, /usr/bin/rm -f /etc/nginx/conf.d/biveit-project-*, /usr/sbin/nginx -t, /usr/bin/systemctl reload nginx
```

## 12. Frontend

```bash
cd ~/workspace/biveit-frontend && npm ci
VITE_API_BASE_URL=/api npm run build
sudo rsync -a --delete dist/ /var/www/biveit/
```

## 13. The backend service

```bash
sudo cp ~/workspace/biveit-backend/deploy/biveit-backend.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now biveit-backend
journalctl -u biveit-backend -f
```

The unit runs the venv's Python as `ubuntu`, with `ENV=prod`, after Docker. On start, the backend:
- checks the projects' network exists;
- starts the agent listener on `10.213.0.1:3002`;
- starts the idle sweep, which stops a project's container after 2 hours without activity.

"the agent listener isn't started" in the log means it couldn't reach Docker. Check the `docker` group.

## 14. Reboots

- The databases come back by themselves (`--restart unless-stopped`).
- The projects' containers have no restart policy: after a reboot they stay stopped, and each one starts
  again the next time its project is opened.
- Nothing is lost. All of a project's state lives in its folder.

## 15. Check it

```bash
cd ~/workspace/biveit-backend && ENV=prod uv run python -m scripts.project_status    # the platform at a glance
sudo ss -ltnp
```

`ss` must show only these:
- `0.0.0.0:80`, `0.0.0.0:443`, `0.0.0.0:22` (and their `[::]` twins)
- `127.0.0.1:3001` (API), `127.0.0.1:5432`, `127.0.0.1:6379`, `127.0.0.1:27017` (the databases' published
  ports)
- `10.213.0.1:3002` (agent listener)
- `127.0.0.1:8xxx` / `127.0.0.1:16xxx` (dev servers, once projects run)
- `127.0.0.53` (the system's DNS stub)

Anything else on `0.0.0.0` is a mistake.

Then open https://biveit.example.com, create a project, and chat until the agent starts implementing. Its
preview opens at `https://<project hash>.dev.biveit.example.com`.

With that project's container running (`docker ps` shows `biveit-prod-project-<id>`), check the walls
from the inside:

```bash
c=biveit-prod-project-<id>
docker exec $c whoami                                                         # biveit (ubuntu's uid), not root
docker exec $c sh -c 'tr "\0" "\n" < /proc/$(pgrep -f session_runner | head -1)/environ | grep ANTHROPIC'
                                                  # a runner's env: the proxy's URL and a session token -- never a key
docker exec $c curl -s -o /dev/null -w '%{http_code}\n' -X POST http://10.213.0.1:3002/api/agent/messages   # 403: reachable, needs a token
docker exec $c python3 -c "import socket; socket.create_connection(('10.213.0.2', 27017), 3); print('mongo ok')"
docker exec $c curl -m 3 -s telnet://10.213.0.1:27017 || echo "mongo via the host: blocked (good)"
docker exec $c curl -m 3 -s http://10.213.0.1:3001/ || echo "API: blocked (good)"
docker exec $c curl -m 3 -s telnet://10.213.0.1:5432 || echo "postgres: blocked (good)"
docker exec $c curl -m 3 -s http://169.254.169.254/ || echo "metadata: blocked (good)"
docker exec $c curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/simple/   # 200: internet and DNS work
```

With two projects running, also check one container can't reach the other: `docker exec <one> curl -m
3 http://<other's 10.213.0.x>:8000/` must time out. `docker inspect <other>` shows its IP.

## Redeploying

```bash
cd ~/workspace
git -C biveit-session-runner pull && git -C biveit-boilerplate pull && git -C biveit-backend pull
cd biveit-backend && uv sync
uv run python -m scripts.migrate --target prod --confirm
sudo systemctl restart biveit-backend
# frontend
cd ~/workspace/biveit-frontend && git pull && npm ci && VITE_API_BASE_URL=/api npm run build
sudo rsync -a --delete dist/ /var/www/biveit/
```

- **Backend restarts don't touch the containers.** Runners and dev servers live in there and keep going,
  and open chats reconnect by themselves. Keep migrations backward compatible (add first, rename or drop
  later), so the old code still works while the new code goes in.
- **The session runner updates itself.** A container gets the current checkout installed the next time
  one of its runners starts, because the installed copy's version is a hash of the source. A runner that
  is already running keeps its code until it's restarted (or its project is closed and reopened).
- **The boilerplate** only affects projects that start implementing after the pull. Existing ones keep
  the app they were given.

**A new image** (the Dockerfile changed):
1. Bump `DEVENV_IMAGE`'s tag. Never reuse a tag: projects keep the tag they were created with.
2. Build it on the server (or push it and pull it there).
3. Deploy the backend.

New projects get the new image. To move an existing project over (its container is disposable):

```sql
UPDATE projects SET meta = jsonb_set(meta, '{devenv_image}', '"<new tag>"') WHERE id = <id>;
```

Then `docker rm -f biveit-prod-project-<id>`. The next open recreates the container on the new image.

## Backups

What has to survive:
- **Postgres:** `docker exec biveit-postgres pg_dump -U postgres biveit | gzip > biveit-$(date +%F).sql.gz`
- **Mongo:** `docker exec biveit-mongo mongodump -u root -p <root password> --authenticationDatabase admin --archive --gzip > mongo-$(date +%F).gz`
- **`~/data/storage_root`:** the projects themselves, including their agents' Claude transcripts
  (`.devenv/home`). Skip `node_modules` and `.venv`, which are rebuilt on the next open.
- **`.env.prod`**, above all `PROJECT_SECRET_SEED`.

## Known limits

- **One model key pays for every agent.** There's no per-user metering or cap on the model proxy yet.
- **Dev sites are public.** Anyone with a project's URL can open its preview (`noindex` only).
- **The dev container holds the project's prod secrets** (its JWT secret, admin token), since dev and
  prod secrets aren't separated yet.
- **No Mongo quota per user**, and no per-project disk limit. Watch `df -h` and `docker system df`.
- **pm2 logs inside the projects** (`<project>/<conversation>/logs/`, `.devenv/logs/`) aren't rotated.
- **`scripts.project_status <id>`'s dev server section reads the host's pm2.** It's out of date: the dev
  servers run in the container now, so use `docker exec biveit-prod-project-<id> pm2 ls`.
- **Project ids must stay below 8000.** The dev ports are derived from them.
- **Deploying projects to production** isn't covered here yet.
