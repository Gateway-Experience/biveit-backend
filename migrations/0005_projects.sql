-- projects: a user's web development projects (the sites/apps they build). Owned by exactly one user.
--
-- Ownership points at users.id, which survives guest promotion (same row), so a guest's projects stay
-- theirs after they sign up -- no rows need moving.
-- Soft-deleted via deleted_at: NULL = live. Every read filters on it; nothing is ever hard-deleted.
-- (users are soft-deleted too, so the user_id FK never needs an ON DELETE rule -- it just guarantees
-- the owner exists.)
-- current_port: the port of the project's agent session, as last seen -- a HINT for debugging and quick
-- reads, NOT the source of truth (the running process is; see services/agent/adapter). It can be stale
-- (session died, port reused); services/projects.ensure_session rewrites it from the live process and
-- clears it from any other project still holding the same port, so a port maps to at most one project.
-- updated_at: when the USER last edited the project (the list sorts by it). Never bumped implicitly --
-- only user-facing edit flows set it, so system writes (current_port) don't reshuffle the list.
-- meta: free-form app-level data (settings, flags) until a field earns its own column.
-- Project content (pages, source files, assets, deployments) belongs in its own tables pointing at
-- projects.id, not in columns here.

CREATE TABLE IF NOT EXISTS projects
(
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT       NOT NULL REFERENCES users (id),  -- owner
    name        varchar(200) NOT NULL,
    description text,
    current_port integer,                                      -- NULL until a session has run
    meta        jsonb        NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ                                    -- NULL = live
);

-- "My projects, most recently edited first" -- the main read. Partial: deleted rows never qualify.
CREATE INDEX IF NOT EXISTS idx_projects_user_updated ON projects (user_id, updated_at DESC) WHERE deleted_at IS NULL;

-- Which project is a session port talking for? (POST /agent/{port}/messages.) Partial: most rows are NULL.
CREATE INDEX IF NOT EXISTS idx_projects_current_port ON projects (current_port) WHERE current_port IS NOT NULL;
