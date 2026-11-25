-- The daily AI limits' settings (services/agent/usage), seeded into system_configurations so they can be tuned
-- without a redeploy. The same values are the code defaults (constants.py, next to ConfigName), which apply when a
-- row is absent or disabled. A row that's already there is left as it is: this never overwrites a value someone has
-- changed.
--   AI_DAILY_LIMIT_USD_PER_USER  USD a user may spend on the model per day (UTC)
--   AI_DAILY_LIMIT_USD_PER_IP    the same per IP group (an IPv4 address / an IPv6 /64)
--   AI_HARD_LIMIT_FACTOR         how far past a daily limit the model proxy cuts calls off (a multiplier)
--   AI_MODEL_PRICES              USD per million tokens, {model name prefix: {input, output, cache_read, cache_write,
--                                peak?}} -- `peak`: the same four, for calls made in AI_PEAK_HOURS
--   AI_UNKNOWN_MODEL_PRICE       what a model matching none of those costs (high on purpose)
--   AI_PEAK_HOURS                when `peak` prices apply, in UTC: {weekdays (ISO, Monday = 1), hours (their start)}

INSERT INTO system_configurations (name, value, meta)
VALUES
    ('AI_DAILY_LIMIT_USD_PER_USER', '2.0', '{"note": "USD per user per day (UTC)"}'),
    ('AI_DAILY_LIMIT_USD_PER_IP', '6.0', '{"note": "USD per IP group per day (UTC)"}'),
    ('AI_HARD_LIMIT_FACTOR', '1.2', '{"note": "the model proxy refuses calls past this times a daily limit"}'),
    ('AI_MODEL_PRICES', '{
        "deepseek-flash": {"input": 0.15, "output": 0.6, "cache_read": 0.003, "cache_write": 0.15,
                           "peak": {"input": 0.3, "output": 1.2, "cache_read": 0.006, "cache_write": 0.3}},
        "deepseek-pro": {"input": 0.66, "output": 1.98, "cache_read": 0.022, "cache_write": 0.66,
                         "peak": {"input": 1.32, "output": 3.96, "cache_read": 0.044, "cache_write": 1.32}},
        "claude-haiku": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
        "claude-sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
        "claude-opus": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25}
    }', '{"note": "USD per million tokens, by model name prefix (the longest matching one wins)"}'),
    ('AI_UNKNOWN_MODEL_PRICE', '{"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25}',
     '{"note": "USD per million tokens for a model not in AI_MODEL_PRICES"}'),
    ('AI_PEAK_HOURS', '{"weekdays": [1, 2, 3, 4, 5], "hours": [1, 2, 3, 6, 7, 8, 9]}',
     '{"note": "UTC: when AI_MODEL_PRICES peak prices apply (DeepSeek: 01-04 and 06-10, Mon-Fri)"}')
ON CONFLICT (name) DO NOTHING;
