-- 002_accounts_and_roles.sql
-- Hierarchy: one ADMIN per institution -> LECTURERs (invited by that admin) -> STUDENTs (self-register).
-- Every account must use an email domain its institution has registered for that kind of account.
-- That rule is a trigger, so no code path (API, CLI, a future admin tool) can bypass it.

ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('ADMIN', 'LECTURER', 'STUDENT'));

-- An account exists before it has a password: lecturers are invited by their admin and students
-- register with just an email; both set a password only after proving they own the mailbox.
ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;
UPDATE users SET password_hash = NULL WHERE password_hash = '!';   -- placeholders left by the demo seed
ALTER TABLE users ADD COLUMN email_verified_at timestamptz;
ALTER TABLE users ADD CONSTRAINT users_verified_has_password
    CHECK (email_verified_at IS NULL OR password_hash IS NOT NULL);

-- exactly one admin per school (zero is allowed only while the school is being set up)
CREATE UNIQUE INDEX one_admin_per_institution ON users (institution_id) WHERE role = 'ADMIN';

-- Which email domains belong to which institution. A domain belongs to exactly one institution, so an
-- email address identifies its school. Managed by the platform operator, not by school admins: a
-- school admin who could add "gmail.com" would let anyone in.
CREATE TABLE institution_email_domains (
    domain         text        PRIMARY KEY
        CHECK (domain = lower(domain) AND domain ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'),
    institution_id uuid        NOT NULL REFERENCES institutions (id),
    allow_staff    boolean     NOT NULL DEFAULT true,    -- admins and lecturers
    allow_students boolean     NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CHECK (allow_staff OR allow_students)
);
CREATE INDEX institution_email_domains_inst_idx ON institution_email_domains (institution_id);

-- existing institutions keep working: their primary domain is allowed for everyone
INSERT INTO institution_email_domains (domain, institution_id) SELECT lower(domain), id FROM institutions;

-- new institutions register their primary domain automatically
CREATE FUNCTION register_primary_domain() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO institution_email_domains (domain, institution_id) VALUES (lower(NEW.domain), NEW.id);
    RETURN NEW;
END $$;
CREATE TRIGGER institutions_register_primary_domain AFTER INSERT ON institutions
    FOR EACH ROW EXECUTE FUNCTION register_primary_domain();

CREATE FUNCTION enforce_user_email_domain() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    d text := lower(split_part(NEW.email, '@', 2));
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM institution_email_domains e
        WHERE e.institution_id = NEW.institution_id AND e.domain = d
          AND ((NEW.role = 'STUDENT' AND e.allow_students) OR (NEW.role <> 'STUDENT' AND e.allow_staff))
    ) THEN
        RAISE EXCEPTION 'email domain "%" is not registered for % accounts at this institution', d, NEW.role
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER users_email_domain BEFORE INSERT OR UPDATE OF email, role, institution_id ON users
    FOR EACH ROW EXECUTE FUNCTION enforce_user_email_domain();

-- One-time codes sent by email: account activation (lecturer invite, student sign-up) and password reset.
-- Only an HMAC of the code is stored. Codes expire, and die after too many wrong guesses.
CREATE TABLE email_codes (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    purpose     text        NOT NULL CHECK (purpose IN ('ACTIVATE', 'RESET_PASSWORD')),
    code_hash   text        NOT NULL,
    expires_at  timestamptz NOT NULL,
    attempts    integer     NOT NULL DEFAULT 0,
    consumed_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX email_codes_lookup_idx ON email_codes (user_id, purpose, created_at DESC);

-- Rotating refresh tokens. The token itself is never stored, only its SHA-256. Using an already-rotated
-- token means it leaked, so the whole family (every token descended from that login) is revoked.
CREATE TABLE refresh_tokens (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    family_id   uuid        NOT NULL,
    token_hash  text        NOT NULL UNIQUE,
    expires_at  timestamptz NOT NULL,
    revoked_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX refresh_tokens_user_idx ON refresh_tokens (user_id);
CREATE INDEX refresh_tokens_family_idx ON refresh_tokens (family_id);
