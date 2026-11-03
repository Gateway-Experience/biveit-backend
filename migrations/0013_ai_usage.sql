-- ai_usage: one row per model call an agent made through our model proxy (services/agent/usage) -- the tokens it
-- used, who it's charged to, and the IP it's counted against. Append-only: nothing is ever updated, so "spent today"
-- is always a sum over the rows (priced at read time, so a price change re-prices nothing stored). It's what the
-- daily AI limits read.
--   ip_group   the IP of the conversation's latest user message at the time of the call, grouped: an IPv4 address
--              as is, an IPv6 one as its /64 (one customer's block). NULL when there's no user message to go by.

CREATE TABLE IF NOT EXISTS ai_usage
(
    id                 BIGSERIAL PRIMARY KEY,
    user_id            BIGINT       NOT NULL,
    project_id         BIGINT       NOT NULL,
    conversation_id    BIGINT       NOT NULL,
    ip_group           varchar(64),
    model              varchar(100) NOT NULL,
    input_tokens       BIGINT       NOT NULL DEFAULT 0,  -- fresh input, not from the cache
    output_tokens      BIGINT       NOT NULL DEFAULT 0,
    cache_read_tokens  BIGINT       NOT NULL DEFAULT 0,
    cache_write_tokens BIGINT       NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- The two reads: a user's spend since some time, an IP group's spend since some time.
CREATE INDEX IF NOT EXISTS idx_ai_usage_user ON ai_usage (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ai_usage_ip_group ON ai_usage (ip_group, created_at) WHERE ip_group IS NOT NULL;
