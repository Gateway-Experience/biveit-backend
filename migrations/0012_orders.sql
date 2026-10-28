-- orders: the payments module's one-off purchases (modules/payments) -- the same idea as `subscriptions` (see
-- 0011_subscriptions.sql): our copy of what the payment provider reports, kept current by its webhooks; the raw
-- webhooks are in `events`; what a user may do is derived from these rows, never stored.
-- target_type + target_id: what it was bought for (PROJECT + the project's id).
-- custom_data: what our checkout attached, kept from the first webhook. snapshot: the provider's object as last
-- fetched.

-- One row per one-off order at the provider: the one-time "just the code" purchase. status: paid | refunded |
-- pending | failed (the provider's).
CREATE TABLE IF NOT EXISTS orders
(
    id           BIGSERIAL PRIMARY KEY,
    provider     varchar(32)   NOT NULL,
    provider_ref varchar(64)   NOT NULL,                  -- the provider's order id
    user_id      BIGINT        NOT NULL REFERENCES users (id),
    target_type  varchar(32)   NOT NULL,                  -- PROJECT
    target_id    BIGINT        NOT NULL,
    product      varchar(16)   NOT NULL,                  -- one_time
    status       varchar(32)   NOT NULL,
    total_usd    numeric(10, 2),
    custom_data  jsonb         NOT NULL DEFAULT '{}',
    snapshot     jsonb         NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),    -- when it was paid (the provider's created_at)
    updated_at   TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_provider_ref ON orders (provider, provider_ref);
CREATE INDEX IF NOT EXISTS idx_orders_target ON orders (target_type, target_id, created_at);
