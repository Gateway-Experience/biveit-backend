# Intentions: the OAuth/OIDC clients for third-party login, via Authlib's Starlette integration. Just
# the provider registration -- the redirect/callback endpoints live in controllers/auth.
#
# Authlib does the protocol work: `state` (CSRF) and the OIDC `nonce` are generated on authorize_redirect
# and checked on authorize_access_token, PKCE (S256) is added per client_kwargs, and the ID token is
# verified against Google's published keys. It keeps that per-login state in request.session, which is
# why the app mounts SessionMiddleware.

from authlib.integrations.starlette_client import OAuth

from src.config import settings

oauth = OAuth()

oauth.register(
    name="google",
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_id=settings.google_client_id,
    client_secret=settings.google_client_secret,
    client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
    # Always show Google's account chooser. Without it, someone already signed in to Google who has
    # approved the app goes straight through with no prompt -- so after logging out of OUR app, the next
    # click on "Continue with Google" (on a shared machine: the next person's) silently signs the same
    # account back in.
    authorize_params={"prompt": "select_account"},
)
