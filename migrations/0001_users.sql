-- users: the app's accounts, both credentialed (USER) and anonymous (GUEST). A guest is promoted to a
-- USER IN PLACE (same id), so everything attached to it survives signup -- see src/services/users/.
--
-- username IS the account's email, for password and Google accounts alike (the FE labels it "email").
-- One email = one account: there's no merging by email until signups are email-verified, so a Google
-- sign-up whose email is already a username is refused (src/services/users/google.py).
-- How the account signs in is a type-ID pair (platform, platform_id), UNIQUE together:
--   WEB    -> platform_id = the username (email/password login)
--   GOOGLE -> platform_id = Google's OIDC `sub` (stable account id, not the email)
--   guests have neither (NULL/NULL) until they sign up.
-- A new sign-in method is a new platform value, not a new column. Note platform_id duplicates username
-- for WEB accounts: anything that changes a WEB user's username must change platform_id with it.
-- username and (platform, platform_id) are UNIQUE but nullable: Postgres treats NULLs as distinct, so
-- any number of guests coexist while real values stay unique.
-- password_hash is bcrypt, never raw, and NULL for guests and Google-only accounts.
-- meta: free-form app-level data, including any FE-sent profile keys that have no column of their own.
-- No Google tokens or raw profile payloads are stored.

CREATE TABLE IF NOT EXISTS users
(
    id              BIGSERIAL PRIMARY KEY,
    user_type       varchar(32)  NOT NULL DEFAULT 'USER',  -- USER | GUEST
    username        varchar(255) UNIQUE,                   -- the email; NULL for guests
    password_hash   varchar(255),
    platform        varchar(32),                           -- WEB | GOOGLE (UserPlatform); NULL for guests
    platform_id     varchar(255),                          -- WEB: the username; GOOGLE: the `sub`
    full_name       varchar(255),
    profile_picture varchar(1000),
    tags            text[]       NOT NULL DEFAULT '{}',
    meta            jsonb        NOT NULL DEFAULT '{}',
    deleted         boolean      NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),

    UNIQUE (platform, platform_id)
);
