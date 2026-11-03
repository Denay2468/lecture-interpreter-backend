-- English lectures still use transcription/playback, but skip paid MT.
ALTER TABLE lecture_sessions DROP CONSTRAINT IF EXISTS sessions_languages_differ;
