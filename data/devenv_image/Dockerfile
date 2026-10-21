# The common image every project's container runs (services/devenv/containers): what the project's dev
# servers, its Redis, its agent's session runner and the agent's tools need from the system. Nothing
# project-specific and nothing secret is baked in -- the project's folder is mounted at run time, the
# runner is copied in by the backend. Built and pushed by scripts/build_devenv_image.sh; DEVENV_IMAGE in
# src/constants.py names the tag new projects get (each project keeps the tag it was created with).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git procps redis-server \
    && rm -rf /var/lib/apt/lists/*

# Node from the official image: Debian's is too old for the boilerplate's Vite.
COPY --from=node:24-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:24-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install -g pm2@7.0.4 \
    && npm cache clean --force

COPY --from=ghcr.io/astral-sh/uv:0.6.12 /uv /uvx /usr/local/bin/

# Chromium + its system libraries, for the Playwright MCP (installed with the session runner). The
# version MUST be the one the runner's pinned @playwright/mcp depends on, or it looks for another browser
# build: @playwright/mcp 0.0.83 -> playwright 1.64.0-alpha-1790635538000. Shared and read-only for every
# uid; the MCP runs it with --no-sandbox (Chromium's sandbox needs privileges the container drops).
ARG PLAYWRIGHT_VERSION=1.64.0-alpha-1790635538000
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers
RUN npx -y "playwright@${PLAYWRIGHT_VERSION}" install --with-deps chromium \
    && chmod -R a+rX /opt/playwright-browsers \
    && rm -rf /root/.npm /var/lib/apt/lists/*
