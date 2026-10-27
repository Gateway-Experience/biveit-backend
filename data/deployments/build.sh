#!/usr/bin/env bash
# Builds a site's apps (modules/deployments/services/images), in the throwaway build container: the devenv
# image, unprivileged, on the projects' network, the commit's backend/ and frontend/ at /app. Every line here
# runs the project's own code (npm scripts, the Vite config, Python build hooks) -- which is why it happens in
# there and not in `docker build`. The site's Dockerfile then copies the results out of this container.
set -euo pipefail

cd /app/frontend
npm ci --no-audit --no-fund
npm run build

# The venv's paths are absolute, so it has to be built where the site runs it: /app/backend.
cd /app/backend
uv sync --frozen --no-dev --no-install-project
