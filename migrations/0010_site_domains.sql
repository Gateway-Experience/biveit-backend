-- site_domains: the domains a project's production site is served on (modules/deployments) -- one row per domain,
-- the user's own and subdomains of ours alike. A domain belongs to at most ONE project, platform-wide (the unique
-- index): two projects claiming the same name would mean two nginx sites fighting over it, or someone attaching
-- another user's domain.
--
-- Hard-deleted: a row goes when the domain is taken off the site, which frees the name for anyone. A trashed or
-- deleted project keeps its rows (and so its names) until something takes them off -- nothing does yet.
-- What a deployment actually serves is read from here when it rolls out; changes apply at the next deployment.
--
-- A site can have several domains attached, but is served on exactly ONE of them at a time -- the active one.
-- kind: how the domain got there, which decides what's needed before it's served (modules/deployments):
--   purchased  bought through us (we hold it at our registrar and set its DNS)
--   external   the user's own -- served only once its ownership proof (a TXT record) checks out
--   admin      set by an admin by hand (trusted, no proof)
-- activated_at: when it became the site's active domain; NULL = attached but not served. At most one per project
-- (the partial unique index). A deployment that started before it means the site still needs a redeploy to
-- serve it.

CREATE TABLE IF NOT EXISTS site_domains
(
    id           BIGSERIAL PRIMARY KEY,
    project_id   BIGINT       NOT NULL REFERENCES projects (id),
    domain       varchar(253) NOT NULL,                   -- lower case, no trailing dot (the module normalizes)
    kind         varchar(16)  NOT NULL DEFAULT 'admin',   -- purchased | external | admin
    activated_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_site_domains_domain ON site_domains (domain);
-- "A project's domains", oldest first.
CREATE INDEX IF NOT EXISTS idx_site_domains_project ON site_domains (project_id, created_at, id);
-- "A project's active domain": at most one.
CREATE UNIQUE INDEX IF NOT EXISTS uq_site_domains_active ON site_domains (project_id) WHERE activated_at IS NOT NULL;

