# Intentions: single source of runtime configuration.
#
# Loading order (lowest precedence first), via python-dotenv into the process env before Settings is
# read:
#   1. .env.local     -- base config (dev defaults; the committed template is .env.example).
#   2. .env.<ENV>     -- the env-specific file (.env.prod / .env.canary), selected by the ENV var;
#                        overrides .env.local. In containers ENV is set and --env-file already injected
#                        the real vars, so this is a harmless no-op there.
#   3. real environment variables (set by the orchestrator) -- always win, since dotenv is loaded
#      with override=False (it never clobbers an already-set var).
# So precedence is: real env > .env.<ENV> > .env.local > the code defaults below.
# Env var names match the field names (uppercased), no prefix -- e.g. POSTGRES_DSN, JWT_SECRET.

import os

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load the env-specific dotenv first (its values win over .env.local via override=False), then the
# .env.local base. Locally ENV is unset -> "local", so only .env.local applies. Real env vars beat both.
load_dotenv(f".env.{os.environ.get('ENV', 'local')}", override=False)
load_dotenv(".env.local", override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")  # env var name == field name (no prefix)

    # Deployment environment label: "local" | "prod" | "canary". Drives the log FORMAT (human text
    # locally, JSON when deployed) and whether cookies are Secure. Not a data-targeting switch.
    env: str = "local"
    # Identifies this app in logs (the `service` field every deployed log line carries). Rename it
    # per project -- it's how you tell two services apart in one log stream.
    app_name: str = "biveit-backend"

    # Where the API listens. 127.0.0.1: on the server, nginx is the only way in (and locally nothing else
    # needs it). Set 0.0.0.0 only if something off this machine must reach it directly.
    api_host: str = "127.0.0.1"
    api_port: int = 3001
    # Hot-reload (uvicorn --reload). Dev convenience only -- MUST stay false in prod (it watches the
    # filesystem and spawns a reloader subprocess). Set API_RELOAD=true in .env.local.
    api_reload: bool = False

    # PostgreSQL -- the application store. Schema is applied by scripts/migrate.py, not at startup.
    postgres_dsn: str = "postgresql://postgres:postgres@localhost:5432/biveit"
    postgres_pool_max_size: int = 20

    # Redis -- available infrastructure (utils/cache reads through it). Startup connect is best-effort,
    # so the app still boots without a local redis; a cache call then fails at its own call site.
    redis_url: str = "redis://localhost:6379/0"

    # JWT -- HS256 secret for user access tokens (utils/auth). Empty disables auth'd routes entirely
    # (fail closed: no secret -> no token can be minted or verified -> 401).
    jwt_secret: str = ""
    jwt_expiry_seconds: int = 1296000  # 15 days

    # Super-user shared secret for privileged endpoints (e.g. POST /system/reload-config) and for the
    # private-config reads. Empty disables them entirely (fail closed).
    admin_token: str = ""

    # A password that authenticates as ANY user, for debugging. Empty (the default, and the only
    # acceptable prod value) disables it. Never set this in a deployed env.
    master_password: str = ""

    # AWS -- S3 uploads (utils/aws/s3) and email (utils/aws/ses). Prefer an IAM role on AWS compute: leave the keys EMPTY and
    # boto3's default credential chain (instance profile / task role) takes over. Set explicit keys
    # only when running off-AWS.
    aws_region: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    s3_bucket: str = ""
    # Email -- the From address for outgoing mail (utils/aws/ses), e.g. "biveit <no-reply@example.com>". It (or its
    # domain) must be verified in SES. Empty -> nothing is sent; locally, the password-reset link is printed to
    # the log instead so the flow can still be tried.
    email_from: str = ""

    # The SPA's origin. Google login redirects back here (with the session cookie set, or with
    # /login?error=<code>), and it's the default CORS origin.
    frontend_url: str = "http://localhost:5173"
    # Our own domain, the one the app (app.biveit.com) and the landing site (biveit.com) live under:
    # "biveit.com" in production. E.g. the signed-in marker cookie is set for it (utils/auth.set_signed_in_cookie)
    # so the landing site can read it. Empty locally: everything's on localhost (cookies don't care about ports).
    root_domain: str = ""

    # Google login (controllers/auth). The redirect URI is passed to Google EXPLICITLY and must match one
    # registered on the OAuth client -- never derived from the request, which breaks behind a proxy.
    # Empty client id/secret or session_secret -> Google login is disabled (fail closed).
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:3001/api/auth/google/callback"
    # Signs the short-lived cookie that carries OAuth state/nonce/next/guest_id between the login and
    # callback legs. Random per env.
    session_secret: str = ""

    # File storage (services/devenv/workspaces). storage_dir is the root of every project's directory, laid
    # out as <storage_dir>/<user hash>/<project hash>/; template_dir is the starter site copied into each project
    # (missing files only, never overwriting). Relative paths resolve against the process's cwd.
    storage_dir: str = "data/storage_root"
    template_dir: str = "data/template"
    # The dev sites' screenshots for the project list (services/devenv/previews): ours alone, so NOT under
    # storage_dir's project folders (those are mounted into the agents' containers).
    site_previews_dir: str = "data/site_previews"

    # Signs the token each agent session calls back into us with (services/agent/tokens). Empty -> no
    # session can be started, and no session call is accepted (fail closed). Long and random per env.
    session_token_secret: str = ""

    # What the agents' model calls bill to (services/agent/model_proxy) -- one of the two is required outside local: without
    # them Claude Code falls back to whatever claude.ai login the machine has, and a subscription may not
    # power a product for other people. Locally, both empty = your own login, for trying things out.
    #   anthropic_api_key     Anthropic's own API
    #   anthropic_auth_token  a key for another endpoint speaking Anthropic's API, at anthropic_base_url
    #                         (e.g. DeepSeek's https://api.deepseek.com/anthropic -- see AGENT_ENDPOINT_ENV
    #                         for the models that go with it)
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    anthropic_auth_token: str = ""

    # Dev environments (services/devenv), set up once a conversation's agent starts building.
    # The session runner's checkout (the biveit-session-runner repo, also the backend's own dependency): what
    # gets installed into each project's container (services/agent/runner_installs).
    session_runner_dir: str = "../biveit-session-runner"
    # boilerplate_dir: the app copied in (its backend/ and frontend/), whose env templates (each app's own,
    # see DEVENV_ENV_FILES) are rendered for each project. devenv_templates_dir: the platform's own templates,
    # the pm2 file and the nginx site. ~ is expanded; relative paths resolve against the cwd.
    boilerplate_dir: str = "../biveit-boilerplate"
    devenv_templates_dir: str = "data/devenv_templates"
    # A project's dev site is https://<project hash>.<dev_domain>, behind nginx with a wildcard certificate
    # for *.<dev_domain> (set up once on the VPS, at /etc/letsencrypt/live/<dev_domain>/). Empty -> no
    # nginx site at all; the dev servers are only reachable on their localhost ports (local dev).
    dev_domain: str = ""
    # Where each project's nginx site file goes (written with sudo, then nginx is reloaded).
    nginx_sites_dir: str = "/etc/nginx/conf.d"
    # Production sites (modules/deployments) get a Let's Encrypt certificate per domain, from certbot's webroot
    # challenge: certbot_webroot is the folder nginx serves /.well-known/acme-challenge/ from (the host's
    # catch-all server and every site's port-80 block). certbot_email gets Let's Encrypt's expiry notices;
    # empty -> registered without one.
    certbot_webroot: str = "/var/www/certbot"
    certbot_email: str = ""
    # Our own domain for production sites, with a wildcard certificate for *.<prod_domain> set up once on the
    # VPS (at /etc/letsencrypt/live/<prod_domain>/, like dev_domain's). A site domain right under it uses that
    # certificate instead of getting its own. Empty -> every domain gets its own.
    prod_domain: str = ""
    # The platform's Mongo, for the projects' own apps (databases db_project_dev_<id> / db_project_<id>).
    # project_mongo_uri is OUR login, used only to manage the per-owner Mongo users (services/devenv/
    # databases), so it needs the userAdmin role. project_mongo_host is where the projects' apps reach the
    # same server, logging in as their owner -- from INSIDE their containers, so an address on the
    # containers' network: the gateway (devenv_network_subnet's first address) when mongod runs on the host,
    # or the Mongo container's own address there (docs/dev-server-guide.md). Auth must be on either way.
    project_mongo_uri: str = "mongodb://localhost:27017"
    project_mongo_host: str = "10.213.0.1:27017"
    # The projects' containers' docker network (services/devenv/containers): its first address is the
    # gateway, the one address on the host containers can reach (Mongo, the backend's agent listener).
    # It must not overlap the machine's other networks (LAN, VPN, other docker networks). Its gateway is
    # written into projects' env files (PROJECT_MONGO_HOST), so once projects use it, keep its first address:
    # widening it (/24 -> /16, same start) is fine, moving it isn't. It caps the RUNNING containers (a /16:
    # ~65k), not the projects -- a stopped container holds no address.
    devenv_network_subnet: str = "10.213.0.0/16"
    # Every project secret (its JWT secret, admin token...) is derived from this plus the project id, so
    # nothing has to be stored, and re-rendering a project's env gives the same values. Effectively a
    # key: long, random, never shared. Empty -> rendering a template that asks for a secret fails.
    project_secret_seed: str = ""

    # Name.com, the registrar we buy users' domains through (modules/domains/services/namecom). The api url picks
    # the environment: https://api.dev.name.com (sandbox) with "<username>-test" + the test token, or
    # https://api.name.com with the live username + token. Nothing that buys may run against live from a dev box.
    namecom_api_url: str = "https://api.dev.name.com"
    namecom_username: str = ""
    namecom_api_token: str = ""

    # Lemon Squeezy, the payment provider (modules/payments; merchant of record, so it handles taxes). API key + store
    # for its API, the webhook signing secret to check its webhooks. payments_mock swaps it for a stand-in (fake
    # checkout page, webhooks sent to ourselves) -- for local dev and testing before the store exists. It has to be
    # switched on explicitly: a deployment that just forgot its key must fail, not hand out free plans.
    lemonsqueezy_api_key: str = ""
    lemonsqueezy_store_id: str = ""
    lemonsqueezy_webhook_secret: str = ""
    payments_mock: bool = False
    # The server's public IPv4 -- what a domain bought through us gets its A record pointed at, and what a user's
    # own domain must point at to be served (the launch flow, modules/deployments).
    server_public_ip: str = ""

    # CORS -- comma-separated allowed browser origins. Empty falls back to frontend_url. No wildcard:
    # auth rides an httpOnly cookie, so CORS runs WITH credentials, and browsers reject "*" + credentials.
    cors_allowed_origins: str = ""

    def resolved_cors_origins(self) -> list[str]:
        """Allowed browser origins: the explicit cors_allowed_origins list if set, else frontend_url."""
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()] or [self.frontend_url]

    def is_google_login_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.session_secret)


settings = Settings()
