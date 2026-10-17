-- project_folders: folders a user makes to group their projects in the sidebar. Owned by one user; a
-- project sits in at most one folder (projects.folder_id), and folders don't nest.
--
-- Soft-deleted via deleted_at: NULL = live. Deleting a folder does NOT touch its projects' folder_id --
-- a project pointing at a deleted folder simply counts as not being in one (the FE groups projects
-- under live folders only). So nothing has to be kept in step, and restoring a folder by hand (clear
-- deleted_at) brings its projects back with it.
-- That a project's folder belongs to the project's own owner is checked by the app on every move
-- (controllers/projects), not by the DB.

CREATE TABLE IF NOT EXISTS project_folders
(
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT       NOT NULL REFERENCES users (id),  -- owner
    name       varchar(200) NOT NULL,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ                                    -- NULL = live
);

-- "My folders" -- the only read. Partial: deleted rows never qualify.
CREATE INDEX IF NOT EXISTS idx_project_folders_user ON project_folders (user_id) WHERE deleted_at IS NULL;

-- Lives here rather than in 0005_projects.sql because it points at the table above. Documented there.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS folder_id BIGINT REFERENCES project_folders (id);
