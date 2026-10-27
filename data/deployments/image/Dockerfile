# The image a project's site runs in production (modules/deployments), built per deployment from this folder
# alone. The project's apps come in already built, from BUILD_IMAGE: the build container (data/deployments/
# build.sh) committed as an image. So nothing here runs the project's code -- every RUN below is ours, and the
# system layers are shared by every project's image.

ARG BUILD_IMAGE
FROM ${BUILD_IMAGE} AS build

# The same base as the devenv image the apps were built in (data/devenv_image): the venv points at this
# Python, at this path. Keep the two in step.
FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx redis-server \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home site \
    && mkdir /data && chown site:site /data
ENV PYTHONUNBUFFERED=1
# Root-owned, so the site's user can't change its own code.
COPY --from=build --chown=0:0 /app/backend /app/backend
COPY --from=build --chown=0:0 /app/frontend/dist /app/frontend/dist
COPY nginx.conf /etc/site/nginx.conf
COPY --chmod=755 entrypoint.sh /usr/local/bin/site-entrypoint
# /data is the site's volume (Redis, anything the app keeps on disk): a fresh named volume copies this
# folder's owner, so the site's user can write there.
USER site
WORKDIR /app/backend
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/site-entrypoint"]
