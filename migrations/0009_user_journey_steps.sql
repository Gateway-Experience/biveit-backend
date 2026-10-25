-- user_journey_steps: the steps a user has taken through one of the FE's journeys (the flows it walks a
-- user through step by step, e.g. the projects page's first-time intro). One row per step, appended as
-- the user takes it -- so a journey's rows, oldest first, are the path they took, and the latest live row
-- is where they are now. Nothing else stores the current step: it's always read off this log, so there's
-- nothing to keep in sync.
--
-- The journey's shape (which steps exist, what may follow what -- could be a whole DAG) belongs to whoever
-- designs it, i.e. the FE. The backend only knows the journey names (constants.UserJourney); `step` is
-- free text and `data` is whatever that step wants to keep (an answer the user gave, say).
-- A finished (or skipped) journey is just a step that ends it, e.g. step = 'finished'.
--
-- Soft-deleted via deleted_at: NULL = live. Going back a step = deleting the latest one(s), so the one
-- before becomes the latest again. Every read filters on it.
-- (users are soft-deleted too, so the user_id FK never needs an ON DELETE rule.)

CREATE TABLE IF NOT EXISTS user_journey_steps
(
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT       NOT NULL REFERENCES users (id),
    journey    varchar(64)  NOT NULL,                     -- UserJourney
    step       varchar(100) NOT NULL,                     -- named by the journey's designer
    data       jsonb        NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ                                -- NULL = live
);

-- "A user's path through a journey" and "where they are now in each": both lead with user_id, journey.
CREATE INDEX IF NOT EXISTS idx_user_journey_steps_user_journey
    ON user_journey_steps (user_id, journey, created_at DESC, id DESC)
    WHERE deleted_at IS NULL;
