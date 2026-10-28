# The deployments module's own constants -- kept here rather than in src/constants.py because the module is
# meant to stand on its own.

from enum import StrEnum

# A site's container publishes SITE_PORT_BASE + project id on the host's 127.0.0.1 -- the "prod 24000 + id"
# of the port plan (see PROJECT_DEV_BACKEND_PORT_BASE in src/constants.py).
SITE_PORT_BASE = 24000
# Inside the container: nginx on SITE_INTERNAL_PORT (what's published), the backend behind it on
# SITE_INTERNAL_BACKEND_PORT. Both are also written in data/deployments/image/nginx.conf -- keep them in step.
SITE_INTERNAL_PORT = 8080
SITE_INTERNAL_BACKEND_PORT = 8000
SITE_HEALTH_PATH = "/api/debug/health"
SITE_HEALTH_WAIT_SECONDS = 90
# Ceilings, not reservations. A site is a built app (no dev servers, no agent), so far less than a dev container.
SITE_CONTAINER_LIMITS = {"memory": "512m", "cpus": "0.5", "pids": 512, "tmp_size": "64m"}

# Our build files (the Dockerfile and what it copies in), the host's nginx site, and the prod env template --
# the last one is the BOILERPLATE's (relative to BOILERPLATE_DIR): the app knows its own config keys.
SITE_IMAGE_DIR = "data/deployments/image"
SITE_NGINX_TEMPLATE = "data/deployments/nginx-site.conf.tmpl"
SITE_ENV_TEMPLATE = "backend/.env.prod.tmpl"
# The site's apps are built in a throwaway container (services/images): the devenv image running SITE_BUILD_SCRIPT
# as SITE_BUILD_UID, with these ceilings. The whole build (install + compile) has SITE_BUILD_TIMEOUT_SECONDS.
SITE_BUILD_SCRIPT = "data/deployments/build.sh"
SITE_BUILD_UID = 10001
SITE_BUILD_LIMITS = {"memory": "2g", "cpus": "1.0", "pids": 1024, "tmp_size": "256m"}
SITE_BUILD_TIMEOUT_SECONDS = 20 * 60


# How a site's domains got there (site_domains.kind) -- which decides what's needed before one is served.
class SiteDomainKind(StrEnum):
    PURCHASED = "purchased"  # bought through us: we hold it and set its DNS
    EXTERNAL = "external"  # the user's own: served once its ownership proof checks out
    ADMIN = "admin"  # set by an admin by hand: trusted


# A user's own domain proves it's theirs with a TXT record at DOMAIN_PROOF_PREFIX.<domain>. The checks ask public
# resolvers directly, so a freshly added record isn't hidden by the server's cache.
DOMAIN_PROOF_PREFIX = "_biveit-verify"
DOMAIN_CHECK_NAMESERVERS = ["1.1.1.1", "8.8.8.8"]
DOMAIN_CHECK_TIMEOUT_SECONDS = 4

# A user can't start another deployment of the same project within this long of the last one (abuse guard). The
# platform's own deployments (the launch flow, admins) skip it.
DEPLOYMENT_COOLDOWN_SECONDS = 5 * 60

# A deployment tries this many times (each attempt picks up where the last one got to), waiting
# DEPLOYMENT_RETRY_DELAY_SECONDS * attempt in between, before rolling back to what was live.
DEPLOYMENT_ATTEMPTS = 3
DEPLOYMENT_RETRY_DELAY_SECONDS = 15
# Safety net for a backend that dies mid-deployment. Must outlast every attempt plus a rollback.
DEPLOYMENT_LOCK_TTL_SECONDS = 3 * 60 * 60
# How many of a project's site images stay on the host (newest first, plus the live one) -- what a
# rollback can go back to without a rebuild.
DEPLOYMENT_KEPT_IMAGES = 3
# Tails of errors kept in the deployment log (events.value).
DEPLOYMENT_ERROR_CHARS = 2000


class DeploymentEvent(StrEnum):
    """events.type of the deployment log. One deployment = one run id (the events' grouping). Every run
    starts with STARTED and, unless the backend died mid-way, ends with one of the last three."""

    STARTED = "deployment.started"
    ATTEMPT_FAILED = "deployment.attempt_failed"
    SUCCEEDED = "deployment.succeeded"
    ROLLED_BACK = "deployment.rolled_back"  # the new commit never came up; what was live before is back
    FAILED = "deployment.failed"  # nothing to roll back to, or the rollback failed too


DEPLOYMENT_OUTCOMES = [DeploymentEvent.SUCCEEDED, DeploymentEvent.ROLLED_BACK, DeploymentEvent.FAILED]
