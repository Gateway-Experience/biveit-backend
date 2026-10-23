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
-- first_name / last_name: what the user goes by, asked for at signup (Google sign-ups take Google's
-- given/family name). The app shows these instead of the email. last_name can be empty (mononyms, and
-- Google profiles without one); guests have neither.
-- language: the one the user picked in the FE's language picker (it saves it here). The agent talks to
-- them in it.
-- meta: free-form app-level data, including any FE-sent profile keys that have no column of their own.
-- Soft-deleted via deleted_at: NULL = live. Every read filters on it; nothing is ever hard-deleted.
-- No Google tokens or raw profile payloads are stored.
-- hash: a random, unguessable public handle (16 hex chars, filled in by the DB on insert). It names the
-- user's folder on disk (<STORAGE_DIR>/<user hash>/...), so paths and URLs don't give away or enumerate ids.
-- Rows that existed before it was added have hash = id::text (their folders were already named by id).
-- role: what the account may do beyond being a user (UserRole). NULL = a normal user; 'ADMIN' = the
-- platform's operators (the admin dashboard, controllers/admin). Set by hand only -- no endpoint writes it:
--   UPDATE users SET role = 'ADMIN' WHERE username = '<email>';

CREATE TABLE IF NOT EXISTS users
(
    id              BIGSERIAL PRIMARY KEY,
    hash            varchar(32)  NOT NULL UNIQUE DEFAULT left(md5(gen_random_uuid()::text), 16),
    user_type       varchar(32)  NOT NULL DEFAULT 'USER',  -- USER | GUEST
    username        varchar(255) UNIQUE,                   -- the email; NULL for guests
    password_hash   varchar(255),
    platform        varchar(32),                           -- WEB | GOOGLE (UserPlatform); NULL for guests
    platform_id     varchar(255),                          -- WEB: the username; GOOGLE: the `sub`
    first_name      varchar(100),
    last_name       varchar(100),
    profile_picture varchar(1000),
    language        varchar(16)  NOT NULL DEFAULT 'en',    -- UserLanguage: the FE's language picker
    role            varchar(32),                           -- UserRole; NULL = a normal user
    tags            text[]       NOT NULL DEFAULT '{}',
    meta            jsonb        NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ,                           -- NULL = live

    UNIQUE (platform, platform_id)
);
