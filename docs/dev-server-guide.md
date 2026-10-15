# Setting up a server (Ubuntu)

Running vib3 on an Ubuntu server (24.04), with several projects in parallel. Below, `vib3.example.com`
is the platform and `*.dev.vib3.example.com` the projects' dev sites; replace both with your domain.

The platform runs **on the host**, not in a container: its job is to start processes there (agent
sessions in tmux, dev servers in pm2). Only the databases run in Docker.

> **Not ready for outside users yet.** Every agent runs as the same Unix user as the backend, so it can
> read the backend's secrets and other projects' files. Fine for internal testing; per-project users
> come later.

## 1. DNS

Two A records to the server: `vib3.example.com` and `*.dev.vib3.example.com`.

## 2. User and packages

```bash
sudo adduser vib3                                     # everything below runs as vib3 unless it says sudo
sudo apt update && sudo apt install -y git tmux nginx docker.io certbot python3-certbot-dns-<provider>
sudo usermod -aG docker vib3
```

`<provider>` is your DNS host's certbot plugin (cloudflare, digitalocean, route53...); it's needed for
the wildcard certificate.

Then as `vib3` (log in again for the docker group):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh                                    # uv
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash    # nvm
nvm install 22 && npm install -g pm2
```

Install Claude Code, then run `claude` once in a scratch folder to log in and get through onboarding.
Start it once more with `claude --permission-mode auto`. If that shows a one-time screen, answer it,
since agent sessions run in auto mode and nobody answers their screens.

## 3. Databases

```bash
docker run -d --name vib3-postgres --restart unless-stopped -p 127.0.0.1:5432:5432 \
  -e POSTGRES_PASSWORD=<pg password> -e POSTGRES_DB=vib3 postgres:16
docker run -d --name vib3-redis --restart unless-stopped -p 127.0.0.1:6379:6379 \
  redis:7 redis-server --databases 16000 --requirepass <redis password>
docker run -d --name vib3-mongo --restart unless-stopped -p 127.0.0.1:27017:27017 \
  -e MONGO_INITDB_ROOT_USERNAME=root -e MONGO_INITDB_ROOT_PASSWORD=<mongo password> mongo:7
```

`--databases 16000`: every project gets its own Redis databases (dev: `id`, prod: `8000 + id`).

## 4. Code and config

```bash
mkdir -p ~/workspace && cd ~/workspace
git clone <vib3-backend> && git clone <vib3-frontend> && git clone <vib3-boilerplate> vib3-boilerplate
cd vib3-backend && uv sync
cp .env.example .env.prod && cp .env.migration.example .env.migration
```

In `.env.prod`:

```
ENV=prod
API_RELOAD=false
POSTGRES_DSN=postgresql://postgres:<pg password>@localhost:5432/vib3
REDIS_URL=redis://:<redis password>@localhost:6379/0
JWT_SECRET=, SESSION_TOKEN_SECRET=, PROJECT_SECRET_SEED=, ADMIN_TOKEN=   # each: openssl rand -hex 32
FRONTEND_URL=https://vib3.example.com
GOOGLE_REDIRECT_URI=https://vib3.example.com/api/auth/google/callback   # if using Google login
STORAGE_DIR=/home/vib3/data/storage_root
DEV_DOMAIN=dev.vib3.example.com
PROJECT_MONGO_URI=mongodb://root:<mongo password>@localhost:27017/?authSource=admin
PROJECT_REDIS_URL=redis://:<redis password>@localhost:6379
```

In `.env.migration`, set `POSTGRES_DSN_PROD` to the same DSN. Then:

```bash
uv run python -m scripts.migrate --target prod --confirm
```

## 5. Certificates

```bash
# Wildcard for the projects' dev sites. The backend expects it at /etc/letsencrypt/live/dev.vib3.example.com/
sudo certbot certonly --dns-<provider> --dns-<provider>-credentials <credentials file> \
  -d dev.vib3.example.com -d '*.dev.vib3.example.com'
# The platform's own (once its nginx site below is in place)
sudo certbot --nginx -d vib3.example.com
```

## 6. nginx

**The platform's site** (`/etc/nginx/sites-available/vib3`, symlinked into `sites-enabled`):

```nginx
server {
    listen 80;
    server_name vib3.example.com;
    root /var/www/vib3;                       # the frontend's build (step 7)

    location /api/ {
        proxy_pass http://127.0.0.1:3001;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 1h;                # the chat's SSE stream stays open
        client_max_body_size 30m;             # uploads are up to 25 MB a file
    }

    location / {
        try_files $uri /index.html;           # SPA routes
    }
}
```

**Projects' dev sites:** the backend writes one file per project into `/etc/nginx/conf.d/` and reloads
nginx. That needs these commands to run without a password (`sudo visudo -f /etc/sudoers.d/vib3`):

```
vib3 ALL=(root) NOPASSWD: /usr/bin/tee /etc/nginx/conf.d/vib3-project-*, /usr/bin/rm -f /etc/nginx/conf.d/vib3-project-*, /usr/sbin/nginx -t, /usr/bin/systemctl reload nginx
```

## 7. Frontend

```bash
cd ~/workspace/vib3-frontend && npm ci
VITE_API_BASE_URL=/api npm run build
sudo mkdir -p /var/www/vib3 && sudo rsync -a --delete dist/ /var/www/vib3/
```

## 8. Run the backend

```bash
pm2 startup systemd                                    # prints a sudo command: run it
sudo cp ~/workspace/vib3-backend/deploy/vib3-backend.service /etc/systemd/system/
sudo nano /etc/systemd/system/vib3-backend.service     # fill in the <...> placeholders
sudo systemctl daemon-reload && sudo systemctl enable --now vib3-backend
```

Read the comments in the unit file. The key settings:
- **`KillMode=process`:** restarting the backend doesn't kill agent sessions or dev servers.
- **`PATH`:** the backend needs to find `claude`, `tmux`, `pm2`, `uv`, `npm` and `node`.
- **`LANG`:** a locale, so non-ASCII text isn't garbled.

## 9. Firewall

```bash
sudo ufw allow 22 && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw enable
```

Everything else listens on 127.0.0.1 anyway: the API, the databases, the dev servers and the agents'
channels.

## Check it

```bash
cd ~/workspace/vib3-backend && ENV=prod uv run python -m scripts.project_status
journalctl -u vib3-backend -f
```

Then open https://vib3.example.com, create a project, and let the agent build it. The preview opens at
`https://<project hash>.dev.vib3.example.com`.

## Redeploying

```bash
# backend
cd ~/workspace/vib3-backend && git pull && uv sync
uv run python -m scripts.migrate --target prod --confirm
sudo systemctl restart vib3-backend
# frontend
cd ~/workspace/vib3-frontend && git pull && npm ci && VITE_API_BASE_URL=/api npm run build
sudo rsync -a --delete dist/ /var/www/vib3/
```

Agents and dev servers keep running through a backend restart, and open chats reconnect on their own.
Keep migrations backward compatible (add, don't rename or drop in one go), so the old code still runs
while the new code is going in.

## Known limits

- **One Claude account runs every agent:** they all share its usage limits.
- **Nothing shuts down when idle:** each opened project keeps its agent and dev servers running until
  `uv run python -m scripts.stop_project <id>`. Watch memory.
- **Project ids must stay below 8000** (ports and Redis databases are derived from them).
