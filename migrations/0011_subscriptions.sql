-- subscriptions: the payments module's purchases (modules/payments) -- what users have bought from us, as the payment
-- provider (Lemon Squeezy) reports it, recurring or not. The provider is the source of truth; these rows are our
-- copy, kept current by its webhooks (each one re-fetches the object from the provider's API and overwrites the
-- row) and a reconcile. The raw webhooks themselves are in `events` (payment.webhook) -- the history, and our copy
-- of every charge (amounts, invoices and receipts are the provider's business); these rows are the current state.
-- Each checkout we start is a `payment.checkout_created` event; custom_data.checkout_id points back at it.
-- What a user may do (host, edit with the agent, download) is DERIVED from them, never stored.
--
-- billing: recurring (a provider subscription: plans, domains) or lifetime (a one-off order that never renews: the
-- one-time purchase). provider_object: which kind of object provider_ref is at the provider -- subscription or
-- order, two separate id spaces there (so it's part of the key), and which API to re-fetch it from.
-- target_type + target_id: what it was bought for -- PROJECT + the project's id (the only kind so far).
-- custom_data: what our checkout attached (user, project, checkout id...) -- the provider hands it back only in
-- webhooks, so it's kept from the first one we see. snapshot: the provider's object as last fetched.

-- One row per subscription / order at the provider. product: plan (hosting + agent; plan = monthly | yearly),
-- domain (one yearly subscription per domain bought through us) or one_time (just the code; plan = one_time).
-- product_data: what's specific to the product -- {domain: <its name>} for a domain, {} otherwise. status is OURS,
-- whatever the provider (each provider's client maps its own onto it; the raw one stays in snapshot): trialing |
-- active | past_due (all running) | cancelled (running until ends_at) | pending | ended | refunded.
CREATE TABLE IF NOT EXISTS subscriptions
(
    id              BIGSERIAL PRIMARY KEY,
    provider        varchar(32)  NOT NULL,                -- lemonsqueezy
    provider_object varchar(16)  NOT NULL,                -- subscription | order
    provider_ref    varchar(64)  NOT NULL,                -- the provider's id for it
    billing         varchar(16)  NOT NULL,                -- recurring | lifetime
    user_id         BIGINT       NOT NULL REFERENCES users (id),
    target_type     varchar(32)  NOT NULL,                -- PROJECT
    target_id       BIGINT       NOT NULL,
    product         varchar(16)  NOT NULL,                -- plan | domain | one_time
    plan            varchar(16),                          -- monthly | yearly | one_time; NULL for a domain
    product_data    jsonb        NOT NULL DEFAULT '{}',
    status          varchar(32)  NOT NULL,
    renews_at       TIMESTAMPTZ,
    ends_at         TIMESTAMPTZ,                          -- set once cancelled / expired
    trial_ends_at   TIMESTAMPTZ,                          -- a free first year runs as a trial
    custom_data     jsonb        NOT NULL DEFAULT '{}',
    snapshot        jsonb        NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),  -- a lifetime one: when it was paid (the provider's)
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_subscriptions_provider_ref ON subscriptions (provider, provider_object, provider_ref);
CREATE INDEX IF NOT EXISTS idx_subscriptions_target ON subscriptions (target_type, target_id, product, created_at);
