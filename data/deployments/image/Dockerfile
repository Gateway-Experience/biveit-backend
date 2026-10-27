# The image a project's site runs in production (modules/deployments), built per deployment from this folder
# plus app/ -- the project's backend/ and frontend/ at the deployed commit (git archive). This folder is ours;
# the agent's code only ever comes in as app/. Ordered for docker's layer cache: the system layers are shared
# by every project, and a deploy that didn't touch dependencies only rebuilds the code layers.

FROM node:24-slim AS frontend
WORKDIR /app/frontend
COPY app/frontend/package.json app/frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY app/frontend/ ./
RUN npm run build

FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx redis-server \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home site \
    && mkdir /data && chown site:site /data
COPY --from=ghcr.io/astral-sh/uv:0.6.12 /uv /usr/local/bin/
ENV UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app/backend
COPY app/backend/pyproject.toml app/backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app/backend/ ./
COPY --from=frontend /app/frontend/dist /app/frontend/dist
COPY nginx.conf /etc/site/nginx.conf
COPY --chmod=755 entrypoint.sh /usr/local/bin/site-entrypoint
# /data is the site's volume (Redis, anything the app keeps on disk): a fresh named volume copies this
# folder's owner, so the site's user can write there.
USER site
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/site-entrypoint"]
