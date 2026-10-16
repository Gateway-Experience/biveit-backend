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
    app_name: str = "vib3-backend"

    # Where the API listens. 127.0.0.1: on the server, nginx is the only way in (and locally nothing else
    # needs it). Set 0.0.0.0 only if something off this machine must reach it directly.
    api_host: str = "127.0.0.1"
    api_port: int = 3001
    # Hot-reload (uvicorn --reload). Dev convenience only -- MUST stay false in prod (it watches the
    # filesystem and spawns a reloader subprocess). Set API_RELOAD=true in .env.local.
    api_reload: bool = False

    # PostgreSQL -- the application store. Schema is applied by scripts/migrate.py, not at startup.
    postgres_dsn: str = "postgresql://postgres:postgres@localhost:5432/vib3"
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
    # Email -- the From address for outgoing mail (utils/aws/ses), e.g. "Vib3 <no-reply@example.com>". It (or its
    # domain) must be verified in SES. Empty -> nothing is sent; locally, the password-reset link is printed to
    # the log instead so the flow can still be tried.
    email_from: str = ""

    # The SPA's origin. Google login redirects back here (with the session cookie set, or with
    # /login?error=<code>), and it's the default CORS origin.
    frontend_url: str = "http://localhost:5173"

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

    # Signs the token each agent session calls back into us with (services/agent/tokens). Empty -> no
    # session can be started, and no session call is accepted (fail closed). Long and random per env.
    session_token_secret: str = ""

    # Dev environments (services/devenv), set up once a conversation's agent starts implementing.
    # boilerplate_dir: the app copied in (its backend/ and frontend/). devenv_templates_dir: the env, pm2
    # and nginx templates rendered for each project. ~ is expanded; relative paths resolve against the cwd.
    boilerplate_dir: str = "../vib3-boilerplate"
    devenv_templates_dir: str = "data/devenv_templates"
    # A project's dev site is https://<project hash>.<dev_domain>, behind nginx with a wildcard certificate
    # for *.<dev_domain> (set up once on the VPS, at /etc/letsencrypt/live/<dev_domain>/). Empty -> no
    # nginx site at all; the dev servers are only reachable on their localhost ports (local dev).
    dev_domain: str = ""
    # Where each project's nginx site file goes (written with sudo, then nginx is reloaded).
    nginx_sites_dir: str = "/etc/nginx/conf.d"
    # What the projects' own apps connect to: one shared Mongo user (each project gets the databases
    # db_project_dev_<id> / db_project_<id>), and one Redis server where each project gets its own
    # numbered databases (see PROJECT_REDIS_DEV_DB_BASE). The Redis URL has NO database number -- each
    # project's is appended. That server needs `databases 18000` (Redis's default is 16).
    project_mongo_uri: str = "mongodb://localhost:27017"
    project_redis_url: str = "redis://localhost:6379"
    # Every project secret (its JWT secret, admin token...) is derived from this plus the project id, so
    # nothing has to be stored, and re-rendering a project's env gives the same values. Effectively a
    # key: long, random, never shared. Empty -> rendering a template that asks for a secret fails.
    project_secret_seed: str = ""

    # CORS -- comma-separated allowed browser origins. Empty falls back to frontend_url. No wildcard:
    # auth rides an httpOnly cookie, so CORS runs WITH credentials, and browsers reject "*" + credentials.
    cors_allowed_origins: str = ""

    def resolved_cors_origins(self) -> list[str]:
        """Allowed browser origins: the explicit cors_allowed_origins list if set, else frontend_url."""
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()] or [self.frontend_url]

    def is_google_login_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.session_secret)


settings = Settings()
