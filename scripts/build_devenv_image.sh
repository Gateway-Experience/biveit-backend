#!/usr/bin/env bash
# Build the projects' common dev image (data/devenv_image) as DEVENV_IMAGE (src/constants.py), and push it
# with --push. Bump DEVENV_IMAGE's tag whenever the Dockerfile changes: projects keep the tag they were
# created with, so reusing a tag would change existing projects' environment under them.
#
#   scripts/build_devenv_image.sh [--push]
set -euo pipefail
cd "$(dirname "$0")/.."
image=$(uv run python -c 'from src.constants import DEVENV_IMAGE; print(DEVENV_IMAGE)')
docker build -t "$image" data/devenv_image  # the build context is that folder only: nothing else can get in
if [[ "${1:-}" == "--push" ]]; then
    docker push "$image"
fi
echo "built $image"
