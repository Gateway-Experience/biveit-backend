-- projects: a user's web development projects (the sites/apps they build). Owned by exactly one user.
--
-- Ownership points at users.id, which survives guest promotion (same row), so a guest's projects stay
-- theirs after they sign up -- no rows need moving.
-- Soft-deleted via deleted_at: NULL = live. Every read filters on it; nothing is ever hard-deleted.
-- trashed_at: when the user moved it to Trash; NULL = not in Trash. A trashed project drops out of the
-- project list (and can't be opened) but can be restored as it was -- name, pin and folder all kept.
-- Deleting for good (deleted_at) only happens from Trash. Added after the table first shipped: an
-- existing DB needs `ALTER TABLE projects ADD COLUMN IF NOT EXISTS trashed_at TIMESTAMPTZ;` by hand.
-- (users are soft-deleted too, so the user_id FK never needs an ON DELETE rule -- it just guarantees
-- the owner exists.)
-- hash: a random, unguessable public handle (16 hex chars, filled in by the DB on insert). It names the
-- project's folder on disk (<STORAGE_DIR>/<user hash>/<project hash>/...) and its dev site
-- (<hash>.<DEV_DOMAIN>), so paths and URLs don't give away or enumerate ids.
-- Rows that existed before it was added have hash = id::text (their folders were already named by id).
-- stage: where the project is in the website creation process -- planning (chatting with the agent to work
-- out the spec) -> building (the app is set up). Moves forward only, when the user approves the plan
-- (services/agent/stages). It's what the project SHOULD be: the workspace and dev
-- servers are brought in line with it on each open. (Before planning there's the starting form, but
-- that's the FE's alone: the project is only created once the form is submitted.)
-- build_approved_at: when the user first approved the built site (the APPROVE_BUILD approval). Set once
-- and kept: from then on the FE offers payment/deployment, even
-- if the user goes back to more planning or building. A milestone, not a stage -- the stage says
-- where the project is now, this says it got there once.
-- bootstrap_data: the starting form's answers, as the FE sent them when creating the project. Handed
-- to the agent to start the chat with.
-- updated_at: when the USER last edited the project (the list sorts by it). Never bumped implicitly --
-- only user-facing edit flows set it, so system writes don't reshuffle the list.
-- pinned_at: when the user pinned the project to the top of their sidebar; NULL = not pinned. Pinned
-- projects are listed by it, newest pin first. Organising, not editing: it never touches updated_at.
-- folder_id: the user's folder it sits directly in (project_folders); NULL = at the root. Always a live
-- folder or NULL: deleting a folder clears it. Added by 0008_project_folders.sql, since that table comes
-- after this one. Same rule: never touches updated_at.
-- meta: free-form app-level data (settings, flags) until a field earns its own column.
-- Project content (pages, source files, assets, deployments) belongs in its own tables pointing at
-- projects.id, not in columns here.

CREATE TABLE IF NOT EXISTS projects
(
    id          BIGSERIAL PRIMARY KEY,
    hash        varchar(32)  NOT NULL UNIQUE DEFAULT left(md5(gen_random_uuid()::text), 16),
    user_id     BIGINT       NOT NULL REFERENCES users (id),  -- owner
    name        varchar(200) NOT NULL,
    description text,
    stage       varchar(32)  NOT NULL DEFAULT 'planning',      -- ProjectStage
    bootstrap_data jsonb      NOT NULL DEFAULT '{}',          -- the starting form's answers
    build_approved_at TIMESTAMPTZ,                             -- NULL until the user signed off a version
    pinned_at   TIMESTAMPTZ,                                   -- NULL = not pinned
    meta        jsonb        NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    trashed_at  TIMESTAMPTZ,                                   -- NULL = not in Trash
    deleted_at  TIMESTAMPTZ                                    -- NULL = live
);

-- "My projects, most recently edited first" -- the main read. Partial: deleted rows never qualify.
CREATE INDEX IF NOT EXISTS idx_projects_user_updated ON projects (user_id, updated_at DESC) WHERE deleted_at IS NULL;
