-- projects: a user's web development projects (the sites/apps they build). Owned by exactly one user.
--
-- Ownership points at users.id, which survives guest promotion (same row), so a guest's projects stay
-- theirs after they sign up -- no rows need moving.
-- Soft-deleted via deleted_at: NULL = live. Every read filters on it; nothing is ever hard-deleted.
-- (users are soft-deleted too, so the user_id FK never needs an ON DELETE rule -- it just guarantees
-- the owner exists.)
-- meta: free-form app-level data (settings, flags) until a field earns its own column.
-- Project content (pages, source files, assets, deployments) belongs in its own tables pointing at
-- projects.id, not in columns here.

CREATE TABLE IF NOT EXISTS projects
(
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT       NOT NULL REFERENCES users (id),  -- owner
    name        varchar(200) NOT NULL,
    description text,
    meta        jsonb        NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ                                    -- NULL = live
);

-- "My projects, most recently edited first" -- the main read. Partial: deleted rows never qualify.
CREATE INDEX IF NOT EXISTS idx_projects_user_updated ON projects (user_id, updated_at DESC) WHERE deleted_at IS NULL;
