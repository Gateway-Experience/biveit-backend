-- ai_messages: the APPEND-ONLY log of a conversation, one row per message as the LLM API sees it.
-- Rows are never updated: a stream that failed midway is still written (stop_reason = aborted/error),
-- and user answers to the agent's questions/actions arrive as new rows (tool_result blocks).
--
-- content is the API's own array of content blocks (text, thinking, tool_use, tool_result, image, ...),
-- stored VERBATIM -- no type whitelist. Some blocks are opaque state the API wants back byte-for-byte
-- (thinking signatures, compaction), so flattening or filtering would break replaying the conversation.
-- seq numbers messages 1, 2, 3... within a conversation. UNIQUE (conversation_id, seq) doubles as the
-- concurrency guard: two writers racing for the same next seq can't both win.
-- model / stop_reason / usage are the facts the API returns with an assistant message (NULL otherwise);
-- usage is the base fact that token totals and billing get summed from.

CREATE TABLE IF NOT EXISTS ai_messages
(
    id               BIGSERIAL PRIMARY KEY,
    conversation_id  BIGINT       NOT NULL REFERENCES ai_conversations (id),
    seq              INT          NOT NULL,
    role             varchar(16)  NOT NULL,          -- user | assistant
    content          jsonb        NOT NULL,          -- array of content blocks
    model            varchar(100),
    stop_reason      varchar(32),                    -- end_turn | tool_use | max_tokens | refusal | aborted | error
    usage            jsonb,                          -- {input_tokens, output_tokens, cache_read_input_tokens, ...}
    meta             jsonb        NOT NULL DEFAULT '{}',
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),

    UNIQUE (conversation_id, seq)
);
