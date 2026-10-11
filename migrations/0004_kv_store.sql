-- kv_store: generic durable key-value table for state you CAN'T rebuild (sync watermarks, checkpoints).
-- Derived/recomputable data belongs in the redis cache instead. Keys are namespaced strings
-- ('<domain>:<id>:<scope>'); `value` is opaque jsonb owned by the key's namespace. No consumer yet.

CREATE TABLE IF NOT EXISTS kv_store
(
    id         BIGSERIAL PRIMARY KEY,
    key        varchar(300) NOT NULL UNIQUE,
    value      jsonb,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT now()
);
