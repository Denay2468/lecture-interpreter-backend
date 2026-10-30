-- 001_initial_schema.sql
-- Lecture Interpreter: institutions, users, rooms, lecture_sessions.
-- Requires PostgreSQL 13+ (gen_random_uuid() is built in; no extensions needed).
-- Students have no accounts: they join a live session with rooms.join_code.

CREATE TABLE institutions (
    id                        uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name                      text        NOT NULL,
    domain                    text        NOT NULL,                      -- e.g. umy.ac.id
    admin_email               text        NOT NULL,                      -- billing/contact address, not a login
    subscription_tier         text        NOT NULL DEFAULT 'TRIAL',      -- business-defined; no CHECK so tiers can change without a migration
    subscription_status       text        NOT NULL DEFAULT 'TRIAL'
        CHECK (subscription_status IN ('TRIAL', 'ACTIVE', 'PAST_DUE', 'CANCELED', 'SUSPENDED')),
    monthly_audio_limit_hours integer     NOT NULL DEFAULT 10 CHECK (monthly_audio_limit_hours >= 0),
    timezone                  text        NOT NULL DEFAULT 'Asia/Jakarta', -- decides when "the month" starts for the quota
    created_at                timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX institutions_domain_key ON institutions (lower(domain));

CREATE TABLE users (
    id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    institution_id uuid        NOT NULL REFERENCES institutions (id),
    email          text        NOT NULL,
    password_hash  text        NOT NULL,
    full_name      text        NOT NULL,
    role           text        NOT NULL CHECK (role IN ('ADMIN', 'LECTURER')),
    is_active      boolean     NOT NULL DEFAULT true,   -- deactivate instead of deleting: sessions keep pointing at the user
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, institution_id)                         -- target of the composite foreign keys below
);
CREATE UNIQUE INDEX users_email_key ON users (lower(email));
CREATE INDEX users_institution_idx ON users (institution_id);

CREATE TABLE rooms (
    id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    institution_id       uuid        NOT NULL REFERENCES institutions (id),
    room_code            text        NOT NULL,          -- human label, e.g. CS-302; unique per institution
    room_name            text,
    join_code            text        NOT NULL,          -- what students type or scan; globally unique
    assigned_lecturer_id uuid,                          -- default lecturer; does not restrict who may start a session
    created_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, institution_id),
    -- the lecturer must belong to the same institution as the room
    FOREIGN KEY (assigned_lecturer_id, institution_id) REFERENCES users (id, institution_id),
    -- alphabet without I, O, 0, 1 so codes survive being read aloud or off a projector
    CONSTRAINT rooms_join_code_format CHECK (join_code ~ '^[A-HJ-NP-Z2-9]{6,10}$')
);
CREATE UNIQUE INDEX rooms_join_code_key ON rooms (join_code);
CREATE UNIQUE INDEX rooms_institution_code_key ON rooms (institution_id, lower(room_code));

CREATE TABLE lecture_sessions (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),   -- the server session id
    institution_id    uuid        NOT NULL,
    room_id           uuid        NOT NULL,
    lecturer_id       uuid        NOT NULL,
    source_language   text        NOT NULL DEFAULT 'id',                   -- spoken by the lecturer
    target_language   text        NOT NULL DEFAULT 'en',                   -- heard by students
    -- LIVE: running. COMPLETED: the lecturer's connection ended normally.
    -- ABORTED: heartbeats stopped (crash/restart); duration is measured to the last heartbeat.
    status            text        NOT NULL DEFAULT 'LIVE' CHECK (status IN ('LIVE', 'COMPLETED', 'ABORTED')),
    started_at        timestamptz NOT NULL DEFAULT now(),
    last_heartbeat_at timestamptz NOT NULL DEFAULT now(),
    ended_at          timestamptz,
    end_reason        text,
    duration_seconds  integer,                                             -- billable seconds, set exactly once when the session ends
    -- both references include institution_id, so a session can never mix tenants
    FOREIGN KEY (room_id, institution_id)     REFERENCES rooms (id, institution_id),
    FOREIGN KEY (lecturer_id, institution_id) REFERENCES users (id, institution_id),
    CONSTRAINT sessions_language_format  CHECK (source_language ~ '^[a-z]{2,3}$' AND target_language ~ '^[a-z]{2,3}$'),
    CONSTRAINT sessions_languages_differ CHECK (source_language <> target_language),
    CONSTRAINT sessions_live_is_open     CHECK (status <> 'LIVE'
                                                OR (ended_at IS NULL AND duration_seconds IS NULL AND end_reason IS NULL)),
    CONSTRAINT sessions_ended_is_closed  CHECK (status = 'LIVE'
                                                OR (ended_at IS NOT NULL AND duration_seconds IS NOT NULL AND end_reason IS NOT NULL)),
    CONSTRAINT sessions_duration_nonneg  CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    CONSTRAINT sessions_end_after_start  CHECK (ended_at IS NULL OR ended_at >= started_at)
);
-- at most one live lecture per room, enforced by the database rather than by application code
CREATE UNIQUE INDEX one_live_session_per_room ON lecture_sessions (room_id) WHERE status = 'LIVE';
-- monthly usage sums
CREATE INDEX sessions_usage_idx ON lecture_sessions (institution_id, started_at);
-- the stale-session reaper only ever looks at live rows
CREATE INDEX sessions_live_heartbeat_idx ON lecture_sessions (last_heartbeat_at) WHERE status = 'LIVE';
