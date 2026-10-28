-- subscriptions: the payments module's recurring purchases (modules/payments) -- what users have bought from us,
-- as the payment provider (Lemon Squeezy) reports it. The provider is the source of truth; these rows are our copy, kept current by its webhooks
-- (each one re-fetches the object from the provider's API and overwrites the row) and a reconcile. The raw
-- webhooks themselves are in `events` (payment.webhook) -- the history; these rows are the current state.
-- What a user may do (host, edit with the agent, download) is DERIVED from them, never stored.
--
-- target_type + target_id: what it was bought for -- PROJECT + the project's id (the only kind so far).
-- custom_data: what our checkout attached (user, project, launch id...) -- the provider hands it back only in
-- webhooks, so it's kept from the first one we see. snapshot: the provider's object as last fetched.

-- One row per subscription at the provider. product: plan (hosting + agent; plan = monthly | yearly) or domain
-- (one yearly subscription per domain bought through us). product_data: what's specific to the product --
-- {domain: <its name>} for a domain, {} otherwise. status is the provider's:
-- on_trial | active | paused | past_due | unpaid | cancelled (still running until ends_at) | expired.
CREATE TABLE IF NOT EXISTS subscriptions
(
    id            BIGSERIAL PRIMARY KEY,
    provider      varchar(32)  NOT NULL,                  -- lemonsqueezy
    provider_ref  varchar(64)  NOT NULL,                  -- the provider's subscription id
    user_id       BIGINT       NOT NULL REFERENCES users (id),
    target_type   varchar(32)  NOT NULL,                  -- PROJECT
    target_id     BIGINT       NOT NULL,
    product       varchar(16)  NOT NULL,                  -- plan | domain
    plan          varchar(16),                            -- monthly | yearly, for product = plan
    product_data  jsonb        NOT NULL DEFAULT '{}',
    status        varchar(32)  NOT NULL,
    renews_at     TIMESTAMPTZ,
    ends_at       TIMESTAMPTZ,                            -- set once cancelled / expired
    trial_ends_at TIMESTAMPTZ,                            -- a free first year runs as a trial
    custom_data   jsonb        NOT NULL DEFAULT '{}',
    snapshot      jsonb        NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_subscriptions_provider_ref ON subscriptions (provider, provider_ref);
CREATE INDEX IF NOT EXISTS idx_subscriptions_target ON subscriptions (target_type, target_id, product, created_at);
