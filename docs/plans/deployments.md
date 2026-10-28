# Deployments (modules/deployments)

Status: first version built (2026-10-26). The code's headers have the details; this is the overview, the server
setup it needs, and how to check it by hand.

## In one page

- **A deployment = a git commit of the workspace.** `deploy` commits whatever's there (inside the dev container;
  the repo is created on first use, its `.gitignore` comes from `data/template/`) and rolls that commit out in the
  background.
- **Rolling out is a list of "ensure" steps**, each skipped when already done, read from reality:

  | Step | Done when |
  |---|---|
  | image `biveit-<env>-site-<id>:<commit>` (apps built in a sandboxed container, then packaged) | the tag exists (same commit, never built twice) |
  | prod Mongo user `site_<id>` on `db_project_<id>` | always ensured (idempotent) |
  | site container `biveit-<env>-site-<id>` | runs this commit (its label) and answers `/api/debug/health` |
  | nginx site + a certificate per domain (or our wildcard) | certbot keeps valid certs; nginx reloads only on change |

  An attempt that fails is retried (3 attempts, growing pause) and each one picks up where the last got to.
  After the last one, the commit that was live is rolled out again the same way (its image is still there).
- **Nothing about what's live is stored:** docker's labels say what runs, git says whether the workspace changed
  since. The log is the `events` table (`deployment.*`, grouped by run id): history only.
- **The build** runs the project's code (npm scripts, Vite config, Python build hooks), so it happens in a
  throwaway container as sandboxed as a dev container: the devenv image, uid 10001, no capabilities, the projects'
  network, limits, no host folder. `data/deployments/build.sh` builds the frontend and the backend's venv at
  `/app`; the container is committed as a temporary image, and our Dockerfile (`data/deployments/image/`) only
  copies the results out of it -- no project code runs in `docker build`.
- **The site container** runs nginx (built frontend, `/api/` to the backend), the backend and its own Redis. No mount of
  the project's folder, a named volume for `/data`, published on `127.0.0.1:24000+id`, the projects' network,
  `--restart=unless-stopped`, no capabilities.
- **Prod is apart from dev**: its own Mongo user and secrets (`project:<id>:prod:<name>`), rendered from the
  boilerplate's `backend/.env.prod.tmpl` and passed to the container by name -- never written to the workspace.
- **Domains**: the `site_domains` table (a domain is one project's, platform-wide), set with
  `PUT /projects/{id}/domains`, applied at the next deployment. All listed, ours too: a subdomain right under
  `PROD_DOMAIN` uses our wildcard certificate (set up once, like the dev one); any other domain gets its own from
  certbot.

## API

- `POST /projects/{id}/deployments` -> 202 `{run_id, commit}`; 409 `NOTHING_TO_DEPLOY` / `DEPLOYMENT_IN_PROGRESS`
- `GET /projects/{id}/deployments/status` -> `{live, deploying, has_undeployed_changes, can_deploy, domains, urls,
  last_deployment}`
- `GET /projects/{id}/deployments` -> the history, newest first
- `PUT /projects/{id}/domains` `{domains: [...]}` -> `{domains}`; 400 `INVALID_DOMAIN`

## Server setup (once)

1. certbot installed (`apt install certbot`), its webroot: `sudo mkdir -p /var/www/certbot` (`CERTBOT_WEBROOT`).
   Optionally `CERTBOT_EMAIL` for expiry notices.
2. A catch-all port-80 server, so a NEW domain's first challenge is answered before its site exists
   (`/etc/nginx/conf.d/00-acme.conf`):
   ```nginx
   server {
       listen 80 default_server;
       listen [::]:80 default_server;
       server_name _;
       location /.well-known/acme-challenge/ { root /var/www/certbot; }
       location / { return 404; }
   }
   ```
   (If another server already claims `default_server` on 80, put the acme location there instead.)
3. Reload nginx after renewals: `/etc/letsencrypt/renewal-hooks/deploy/reload-nginx` containing
   `#!/bin/sh\nsystemctl reload nginx` (executable). certbot's timer does the renewing.
4. sudoers for the backend's user, on top of what dev sites already have (tee/rm in the sites folder, `nginx -t`,
   `systemctl reload nginx`): `/usr/bin/certbot certonly *`.
5. Mongo listening on the projects' network gateway with auth on -- already needed by dev containers.

## Check it by hand

- Build + run a site from the boilerplate (no project needed): a scratch script calling `images.build_image` on
  `git archive HEAD backend frontend` of the boilerplate, then `containers.ensure_site_container` with
  `environment.render_site_env` -- then `curl 127.0.0.1:<24000+id>/` and `/api/debug/health`.
- Real flow on a project in building: `PUT .../domains`, `POST .../deployments`, poll `.../deployments/status`.
- Rollback: break the backend (a bad import), deploy -> 3 failed attempts, `rolled_back`, the old commit live.

## Not done yet

- Gating (payment / plan) before the FE offers deploying.
- Tearing a site down (project deleted / unpaid): container, volume, images, nginx file.
- Env keys the agent adds in dev (`.env.local`) don't reach prod; only the template's do.
- Prod "elsewhere": every prod command is local docker / sudo today.
- Zero-downtime swaps (a second port per project, blue/green); today it's stop-old, start-new (seconds).
