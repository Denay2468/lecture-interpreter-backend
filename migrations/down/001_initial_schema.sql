-- Development reset only. Destroys all data. Not run by the migration runner.
DROP TABLE IF EXISTS lecture_sessions;
DROP TABLE IF EXISTS rooms;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS institutions;
DELETE FROM schema_migrations WHERE version = '001_initial_schema';
