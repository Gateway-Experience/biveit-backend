-- site_domains: the domains a project's production site is served on (modules/deployments) -- one row per domain,
-- the user's own and subdomains of ours alike. A domain belongs to at most ONE project, platform-wide (the unique
-- index): two projects claiming the same name would mean two nginx sites fighting over it, or someone attaching
-- another user's domain.
--
-- Hard-deleted: a row goes when the domain is taken off the site, which frees the name for anyone. A trashed or
-- deleted project keeps its rows (and so its names) until something takes them off -- nothing does yet.
-- What a deployment actually serves is read from here when it rolls out; changes apply at the next deployment.

CREATE TABLE IF NOT EXISTS site_domains
(
    id         BIGSERIAL PRIMARY KEY,
    project_id BIGINT       NOT NULL REFERENCES projects (id),
    domain     varchar(253) NOT NULL,                     -- lower case, no trailing dot (the module normalizes)
    created_at TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_site_domains_domain ON site_domains (domain);
-- "A project's domains", oldest first.
CREATE INDEX IF NOT EXISTS idx_site_domains_project ON site_domains (project_id, created_at, id);

-- The domains used to live in projects.domains ({"app_domains": [...]}, added by hand after 0005 shipped). Move
-- any that are there, then drop the column. A DB that never had the column skips both.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'projects' AND column_name = 'domains') THEN
        INSERT INTO site_domains (project_id, domain)
        SELECT id, jsonb_array_elements_text(domains -> 'app_domains') FROM projects
        ON CONFLICT (domain) DO NOTHING;
        ALTER TABLE projects DROP COLUMN domains;
    END IF;
END $$;
