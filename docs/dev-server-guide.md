# Setting up a server

The platform on one Ubuntu box, as the `ubuntu` user. Assumes Docker (with `ubuntu` in the `docker`
group), nginx, certbot with your DNS provider's plugin, uv and node are installed. `app.example.com` is
the platform and `*.dev.example.com` the projects' dev sites -- replace both.

| What | Where | Listens on |
|---|---|---|
| backend API | systemd `biveit-backend` | `127.0.0.1:3001` |
| agent listener (agent endpoints + model proxy) | same process | `10.213.0.1:3002` (the projects' gateway) |
| Postgres, Redis | containers | `127.0.0.1` only |
| projects' Mongo | container, host network | `127.0.0.1:7017` + `10.213.0.1:7017` |
| one container per project | Docker network `biveit-projects` (10.213.0.0/16) | dev ports on `127.0.0.1`: 8000 + id, 16000 + id |

A project container mounts only its own folder and its runner sockets, and never holds the model key. It
can't reach other project containers, and on the host it reaches only the agent listener and Mongo.

## 1. Code

The four repos sit side by side. The backend finds the other three by relative path.

```bash
mkdir -p ~/workspace && cd ~/workspace
for repo in biveit-backend biveit-frontend biveit-boilerplate biveit-session-runner; do
  git clone git@github.com:Gateway-Experience/$repo.git
done
cd biveit-backend && uv sync
```

## 2. The projects' network and Mongo

The backend creates `biveit-projects` on its first start. Create it now instead, so Mongo can listen on
its gateway address:

```bash
docker network create --subnet=10.213.0.0/16 --opt com.docker.network.bridge.name=biveit-projects --opt com.docker.network.bridge.enable_icc=false biveit-projects
```

Mongo for the projects' apps, used by nothing else (agent-written code connects to it). Auth is on
because a root user is set. Run these in one shell:

```bash
read -rsp "Mongo root password: " MONGO_INITDB_ROOT_PASSWORD; echo
export MONGO_INITDB_ROOT_PASSWORD MONGO_INITDB_ROOT_USERNAME=root
docker run -d --name biveit-mongo --restart always --network host -v biveit-mongo:/data/db -e MONGO_INITDB_ROOT_USERNAME -e MONGO_INITDB_ROOT_PASSWORD mongo:7.0 --bind_ip 127.0.0.1,10.213.0.1 --port 7017 --wiredTigerCacheSizeGB 1
until ss -ltn | grep -q "10.213.0.1:7017 "; do sleep 1; done
export BIVEIT_MONGO_PW=$(openssl rand -hex 32)
docker exec -e BIVEIT_MONGO_PW biveit-mongo sh -c 'mongosh --port 7017 -u root -p "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin --quiet --eval "db.getSiblingDB(\"admin\").createUser({user: \"biveit\", pwd: process.env.BIVEIT_MONGO_PW, roles: [{role: \"userAdminAnyDatabase\", db: \"admin\"}]}).ok"'
echo "$BIVEIT_MONGO_PW"      # goes into PROJECT_MONGO_URI below
```

- **`-e NAME` without a value** makes Docker read the password from the shell, never from the command
  line.
- **The backend's `biveit` login can only manage users.** It creates one Mongo user per project owner.

## 3. Config

```bash
cp .env.example .env.prod && chmod 600 .env.prod
cp .env.migration.example .env.migration && chmod 600 .env.migration    # POSTGRES_DSN_PROD = the DSN below
mkdir -p ~/data/storage_root
```

In `.env.prod` (anything not listed keeps `.env.example`'s value; each secret is `openssl rand -hex 32`):

```
ENV=prod
API_RELOAD=false
POSTGRES_DSN=postgresql://<user>:<pw>@127.0.0.1:5432/biveit
REDIS_URL=redis://:<pw>@127.0.0.1:6379/0
JWT_SECRET=
SESSION_TOKEN_SECRET=
PROJECT_SECRET_SEED=          # every project's secrets derive from it: set once, back it up
ADMIN_TOKEN=
FRONTEND_URL=https://app.example.com
STORAGE_DIR=/home/ubuntu/data/storage_root
DEV_DOMAIN=dev.example.com
PROJECT_MONGO_URI=mongodb://biveit:<BIVEIT_MONGO_PW>@127.0.0.1:7017/?authSource=admin
PROJECT_MONGO_HOST=10.213.0.1:7017
# Google login (optional): GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, SESSION_SECRET, GOOGLE_REDIRECT_URI
```

```bash
uv run python -m scripts.migrate --target prod --confirm
```

The model key: one `.env`-format file per key set in `.agent_api_keys/` (gitignored), holding
`ANTHROPIC_API_KEY`, or `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`. Then switch to one:

```bash
mkdir -m 700 -p .agent_api_keys          # then e.g. .agent_api_keys/anthropic.env, chmod 600
uv run python -m scripts.manage keys --env prod                         # the sets, and which one is in use
uv run python -m scripts.manage keys use anthropic --env prod --apply   # then restart the backend
```

## 4. The projects' image

```bash
scripts/build_devenv_image.sh        # tags DEVENV_IMAGE (src/constants.py)
```

## 5. Host settings

Every container runs as `ubuntu`'s uid, so all projects share one pool of file watchers:

```bash
printf 'fs.inotify.max_user_watches = 524288\nfs.inotify.max_user_instances = 1024\n' | sudo tee /etc/sysctl.d/90-biveit.conf && sudo sysctl --system
```

Firewall: from the projects' containers, the host lets in only the agent listener and Mongo. Containers
can't reach each other anyway (the network's `enable_icc=false`).

```bash
sudo ufw default deny incoming && sudo ufw default allow outgoing
sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
sudo ufw allow in on biveit-projects to 10.213.0.1 port 3002 proto tcp
sudo ufw allow in on biveit-projects to 10.213.0.1 port 7017 proto tcp
sudo ufw --force enable
```

Never publish a container port on `0.0.0.0`. Docker's published ports go around ufw.

## 6. nginx

The platform's site proxies `/api/` to the API and serves the frontend's build:

```nginx
server {
    server_name app.example.com;
    root /var/www/app.example.com;
    location /api/ {
        proxy_pass http://127.0.0.1:3001;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 1h;          # the chat's SSE stream
        client_max_body_size 30m;       # uploads
    }
    location / { try_files $uri /index.html; }
}
```

```bash
sudo certbot --nginx -d app.example.com
cd ~/workspace/biveit-frontend && npm ci && VITE_API_BASE_URL=/api npm run build && sudo rsync -a --delete dist/ /var/www/app.example.com/
```

**Dev sites.** The backend writes `/etc/nginx/conf.d/biveit-project-<id>.conf` itself, using a wildcard
certificate at `/etc/letsencrypt/live/dev.example.com/`:

```bash
sudo certbot certonly --dns-<provider> --dns-<provider>-credentials <credentials file> -d dev.example.com -d '*.dev.example.com'
```

It needs passwordless sudo for exactly these. On cloud images, `ubuntu` already has it.

```
ubuntu ALL=(root) NOPASSWD: /usr/bin/tee /etc/nginx/conf.d/biveit-project-*, /usr/bin/rm -f /etc/nginx/conf.d/biveit-project-*, /usr/sbin/nginx -t, /usr/bin/systemctl reload nginx
```

## 7. The service

```bash
sudo cp ~/workspace/biveit-backend/deploy/biveit-backend.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now biveit-backend
```

Restarting it doesn't touch the projects' containers.

## Check it

```bash
sudo ss -ltnp        # public: only 22, 80, 443. 10.213.0.1: only 3002 and 7017. The rest on 127.0.0.1
ENV=prod uv run python -m scripts.manage status
```

Open a project and let it start implementing. Then, against its container:

```bash
c=biveit-prod-project-<id>
docker exec $c sh -c 'tr "\0" "\n" < /proc/$(pgrep -f session_runner | head -1)/environ | grep ANTHROPIC'   # a session token, never a key
docker exec $c python3 -c "import socket; socket.create_connection(('10.213.0.1', 7017), 3); print('mongo ok')"
docker exec $c curl -m 3 -s telnet://10.213.0.1:5432 || echo "postgres blocked"
docker exec $c curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/simple/      # 200
```

## Redeploying

```bash
cd ~/workspace && for r in biveit-session-runner biveit-boilerplate biveit-backend; do git -C $r pull; done
cd biveit-backend && uv sync && uv run python -m scripts.migrate --target prod --confirm && sudo systemctl restart biveit-backend
```

- **The runner** updates itself: a container gets the new one the next time one of its runners starts.
- **A new image:** bump `DEVENV_IMAGE`'s tag, never reuse one. Existing projects keep theirs until you
  change `projects.meta.devenv_image` and `docker rm -f` their container.
