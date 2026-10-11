-- system_configurations: runtime, operator-tunable settings (name -> jsonb value) that change behaviour
-- without a redeploy. Two independent gates per row:
--   status     -- whether the value is IN EFFECT (DISABLED = treated as absent -> code default).
--   is_private -- who may READ it (private = super user only). Defaults to private: opt in to public.

CREATE TABLE IF NOT EXISTS system_configurations
(
    id         BIGSERIAL PRIMARY KEY,
    name       varchar(200) NOT NULL UNIQUE,
    value      jsonb,
    status     varchar(32)  NOT NULL DEFAULT 'ENABLED',  -- ENABLED | DISABLED
    is_private boolean      NOT NULL DEFAULT true,
    meta       jsonb        NOT NULL DEFAULT '{}',       -- notes/provenance
    deleted    boolean      NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT now()
);
