#!/bin/bash
# Starts a site's three processes -- its own Redis, nginx in front, the backend -- and exits as soon as any
# one of them does, so docker's restart policy brings the whole site back instead of leaving it half up.
set -euo pipefail
mkdir -p /data/redis
redis-server --bind 127.0.0.1 --port 6379 --dir /data/redis --maxmemory 128mb --maxmemory-policy allkeys-lru &
nginx -c /etc/site/nginx.conf &
(cd /app/backend && exec .venv/bin/python main.py) &
wait -n
exit 1
