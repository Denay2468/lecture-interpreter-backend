-- 003_attendance.sql
-- Which students attended which lecture. One row per student per lecture: a student whose connection drops and
-- comes back is the same row with another connection counted, not a second attendee.

-- lets other tables reference a lecture together with its school, so a row can never mix schools
ALTER TABLE lecture_sessions ADD CONSTRAINT lecture_sessions_id_institution_key UNIQUE (id, institution_id);

CREATE TABLE session_participants (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id        uuid        NOT NULL,
    institution_id    uuid        NOT NULL,
    user_id           uuid        NOT NULL,
    first_joined_at   timestamptz NOT NULL DEFAULT now(),
    last_left_at      timestamptz,
    connected_since   timestamptz,                                    -- set while a connection is open
    connected_seconds integer     NOT NULL DEFAULT 0 CHECK (connected_seconds >= 0),  -- closed connections only
    connections       integer     NOT NULL DEFAULT 1 CHECK (connections >= 1),
    UNIQUE (session_id, user_id),
    FOREIGN KEY (session_id, institution_id) REFERENCES lecture_sessions (id, institution_id),
    FOREIGN KEY (user_id, institution_id)    REFERENCES users (id, institution_id)
);
CREATE INDEX session_participants_user_idx ON session_participants (user_id, first_joined_at DESC);

-- only students are attendees (lecturers and admins teach; they don't attend)
CREATE FUNCTION enforce_participant_is_student() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT role FROM users WHERE id = NEW.user_id) <> 'STUDENT' THEN
        RAISE EXCEPTION 'only students can be recorded as attending a lecture' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER session_participants_student_only BEFORE INSERT ON session_participants
    FOR EACH ROW EXECUTE FUNCTION enforce_participant_is_student();
