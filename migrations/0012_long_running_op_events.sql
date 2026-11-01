-- long_running_op_events: the log of long-running operations (modules/long_running_ops) -- a request that's answered
-- right away with an id and carried on in the background, its caller polling that id for how it went. Append-only
-- (the STREAM half of table/stream): one row per thing that happened to an operation, all of an operation's rows
-- sharing its operation_id. What `value` holds, and how an operation's rows fold into where it stands, is entirely
-- the caller's (the operation's own code) -- this table only keeps them, in order.

CREATE TABLE IF NOT EXISTS long_running_op_events
(
    id           BIGSERIAL PRIMARY KEY,
    operation_id uuid        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    value        jsonb       NOT NULL DEFAULT '{}'
);

-- "An operation's events, in order" -- the only read.
CREATE INDEX IF NOT EXISTS idx_long_running_op_events_operation ON long_running_op_events (operation_id, created_at, id);
