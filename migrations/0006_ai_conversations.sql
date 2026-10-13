-- ai_conversations: one chat thread between a user and the AI agent, about one project.
-- user_id is who STARTED it -- not derived from projects.user_id (the owner), even though the two are
-- the same today; they'll differ once projects can be shared.
-- title stays NULL until someone (the user, or an auto-titler) names it.
-- What was said lives in ai_messages; nothing here summarizes it (e.g. no last_message_at -- that's
-- max(ai_messages.created_at), cheap via its unique index).
-- Soft-deleted via deleted_at: NULL = live.

CREATE TABLE IF NOT EXISTS ai_conversations
(
    id          BIGSERIAL PRIMARY KEY,
    project_id  BIGINT       NOT NULL REFERENCES projects (id),
    user_id     BIGINT       NOT NULL REFERENCES users (id),   -- who started it
    title       varchar(200),
    meta        jsonb        NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ                                     -- NULL = live
);

-- "A project's conversations, newest first" -- also how the current one is picked.
CREATE INDEX IF NOT EXISTS idx_ai_conversations_project ON ai_conversations (project_id, created_at DESC) WHERE deleted_at IS NULL;
