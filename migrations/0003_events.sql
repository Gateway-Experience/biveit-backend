-- events: a generic, append-only activity log (the STREAM half of table/stream). Each participant is a
-- type-ID pair (subject / direct object / indirect object / grouping), so a new kind of event is new
-- type values, not a migration. `value` is the event's own free-form payload. No consumer yet.
-- Every index ends in created_at: these are all "events matching X, in time order" reads.

CREATE TABLE IF NOT EXISTS events
(
    id                   BIGSERIAL PRIMARY KEY,
    type                 varchar(100) NOT NULL,
    subject_type         varchar(100),
    subject_ref          varchar(255),
    direct_object_type   varchar(100),
    direct_object_ref    varchar(255),
    indirect_object_type varchar(100),
    indirect_object_ref  varchar(255),
    grouping_type        varchar(100),
    grouping_ref         varchar(255),
    value                jsonb        NOT NULL DEFAULT '{}',
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_events_type_subject ON events (type, subject_type, subject_ref, created_at);
CREATE INDEX IF NOT EXISTS idx_events_subject_ref ON events (subject_ref, created_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (type, created_at);
CREATE INDEX IF NOT EXISTS idx_events_type_direct_object ON events (type, direct_object_type, direct_object_ref, created_at);
