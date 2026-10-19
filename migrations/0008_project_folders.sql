-- project_folders: folders a user makes to group their projects in the sidebar. Owned by one user; a
-- project sits in at most one folder (projects.folder_id -- the folder it's DIRECTLY in).
--
-- Folders nest: parent_id is the folder this one sits in, NULL = the user's root. A parent is always one
-- of the same user's live folders, and a folder never ends up inside its own subtree -- both checked by
-- the app (services/project_folders), not by the DB.
--
-- Soft-deleted via deleted_at: NULL = live. Deleting a folder deletes its whole subtree with it, all
-- stamped with the same deleted_at -- so there's never a live folder under a deleted one. The projects
-- filed in any of them go back to the root (folder_id cleared, same statement), so a project's folder_id
-- always names a live folder or NULL. Restoring folders by hand (clear deleted_at on the rows with that
-- stamp) therefore brings back empty folders -- which projects were in them isn't kept.
-- That a project's folder belongs to the project's own owner is checked by the app on every move
-- (controllers/projects), not by the DB.

CREATE TABLE IF NOT EXISTS project_folders
(
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT       NOT NULL REFERENCES users (id),            -- owner
    parent_id  BIGINT REFERENCES project_folders (id),                  -- NULL = at the root
    name       varchar(200) NOT NULL,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ                                              -- NULL = live
);

-- Live folder names are unique within the same parent, ignoring case ("Work" and "work" clash). NULLS NOT
-- DISTINCT makes the root (parent_id NULL) count as one parent too -- without it, Postgres would let any
-- number of same-named root folders through. Deleted folders don't count, so a name frees up on delete.
-- Also the index behind "my folders" (user_id leads) and behind walking a subtree (user_id, parent_id).
CREATE UNIQUE INDEX IF NOT EXISTS uq_project_folders_parent_name
    ON project_folders (user_id, parent_id, lower(name)) NULLS NOT DISTINCT
    WHERE deleted_at IS NULL;

-- Lives here rather than in 0005_projects.sql because it points at the table above. Documented there.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS folder_id BIGINT REFERENCES project_folders (id);
