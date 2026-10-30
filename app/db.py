"""Database layer (asyncpg).

Plain SQL in one module, no ORM: four tables and a handful of queries, in an
asyncio app, where the interesting work is a few atomic statements (start a
session with a quota check, complete it exactly once, reap stale ones). If
the schema grows into admin CRUD, this is the only file that would change.

Design rule: the database is never in the audio path. It is consulted when a
lecture starts or ends and by a periodic heartbeat, so a slow or briefly
unavailable database cannot stall a running lecture (see lifecycle.py).
"""
from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import asyncpg

logger = logging.getLogger("db")

JOIN_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I, O, 0, 1
_MIGRATION_LOCK_KEY = 727_001
_STARTABLE_STATUSES = ("TRIAL", "ACTIVE")


def new_join_code(length: int = 6) -> str:
    return "".join(secrets.choice(JOIN_CODE_ALPHABET) for _ in range(length))


def normalize_join_code(raw: str) -> str:
    """What a student types may include spaces, dashes or lowercase."""
    return "".join(ch for ch in raw.upper() if ch.isalnum())


class SessionStartError(Exception):
    """Base class for every reason a lecture cannot start. The message is safe to show the lecturer."""


class RoomNotFound(SessionStartError):
    pass


class InstitutionInactive(SessionStartError):
    pass


class LecturerNotAllowed(SessionStartError):
    pass


class RoomBusy(SessionStartError):
    pass


class QuotaExceeded(SessionStartError):
    def __init__(self, used_seconds: int, limit_seconds: int) -> None:
        super().__init__(f"Monthly audio limit reached ({used_seconds // 60} of {limit_seconds // 60} minutes used)")
        self.used_seconds = used_seconds
        self.limit_seconds = limit_seconds


@dataclass(frozen=True)
class AuthUser:
    id: uuid.UUID
    institution_id: uuid.UUID
    email: str
    full_name: str
    role: str


Lecturer = AuthUser  # the older name, kept so existing imports keep working


@dataclass(frozen=True)
class Room:
    id: uuid.UUID
    institution_id: uuid.UUID
    room_code: str
    join_code: str


@dataclass(frozen=True)
class LiveSession:
    id: uuid.UUID
    institution_id: uuid.UUID
    room_id: uuid.UUID
    lecturer_id: uuid.UUID
    source_language: str
    target_language: str
    started_at: datetime


@dataclass(frozen=True)
class JoinTarget:
    room: Room
    session: Optional[LiveSession]  # None: the code is valid but nothing is live in that room right now


@dataclass(frozen=True)
class Usage:
    used_seconds: int
    limit_seconds: int

    @property
    def remaining_seconds(self) -> int:
        return max(0, self.limit_seconds - self.used_seconds)


# Seconds used this calendar month in the institution's timezone. A running
# session counts for the time elapsed so far, so two lectures running at once
# are both reflected.
_USED_SECONDS_SQL = """
SELECT COALESCE(SUM(CASE WHEN status = 'LIVE'
                         THEN FLOOR(EXTRACT(EPOCH FROM (now() - started_at)))
                         ELSE duration_seconds END), 0)::bigint
FROM lecture_sessions
WHERE institution_id = $1
  AND started_at >= (date_trunc('month', now() AT TIME ZONE $2::text) AT TIME ZONE $2::text)
"""


class Database:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str, *, min_size: int = 1, max_size: int = 5,
                      search_path: Optional[str] = None) -> "Database":
        settings = {"search_path": search_path} if search_path else None
        pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size,
                                         command_timeout=10, server_settings=settings)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool

    # ---- migrations -------------------------------------------------

    async def migrate(self, directory: Path) -> list[str]:
        """Applies pending migrations/*.sql in filename order, each in its own
        transaction, under an advisory lock so two servers starting together
        don't race. Returns the versions applied by this call."""
        applied: list[str] = []
        async with self._pool.acquire() as conn:
            await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_KEY)
            try:
                await conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations "
                    "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
                done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
                for path in sorted(directory.glob("*.sql")):
                    if path.stem in done:
                        continue
                    async with conn.transaction():
                        await conn.execute(path.read_text(encoding="utf-8"))
                        await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", path.stem)
                    applied.append(path.stem)
                    logger.info("Applied migration %s", path.stem)
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_KEY)
        return applied

    # ---- lookups ----------------------------------------------------

    async def get_active_user(self, user_id: uuid.UUID) -> Optional[AuthUser]:
        """The one place that decides whether an account may act right now. It must be active, have a
        verified email, and its email domain must STILL be registered for its kind of account (a school
        that drops a domain locks those accounts out immediately). Used by the HTTP API and by the
        WebSocket handshake, so both enforce exactly the same rule."""
        row = await self._pool.fetchrow(
            "SELECT u.id, u.institution_id, u.email, u.full_name, u.role FROM users u "
            "WHERE u.id = $1 AND u.is_active AND u.email_verified_at IS NOT NULL "
            "  AND EXISTS (SELECT 1 FROM institution_email_domains d "
            "              WHERE d.institution_id = u.institution_id AND d.domain = lower(split_part(u.email, '@', 2)) "
            "                AND ((u.role = 'STUDENT' AND d.allow_students) OR (u.role <> 'STUDENT' AND d.allow_staff)))",
            user_id)
        return AuthUser(**dict(row)) if row else None

    async def get_room_by_label(self, institution_id: uuid.UUID, room_code: str) -> Optional[Room]:
        row = await self._pool.fetchrow(
            "SELECT id, institution_id, room_code, join_code FROM rooms "
            "WHERE institution_id = $1 AND lower(room_code) = lower($2)", institution_id, room_code.strip())
        return Room(**dict(row)) if row else None

    async def create_room(self, institution_id: uuid.UUID, room_code: str, room_name: Optional[str] = None,
                          assigned_lecturer_id: Optional[uuid.UUID] = None) -> Room:
        """Creates a room with a fresh join code, retrying on the (rare) code collision."""
        for _ in range(8):
            try:
                row = await self._pool.fetchrow(
                    "INSERT INTO rooms (institution_id, room_code, room_name, join_code, assigned_lecturer_id) "
                    "VALUES ($1, $2, $3, $4, $5) RETURNING id, institution_id, room_code, join_code",
                    institution_id, room_code, room_name, new_join_code(), assigned_lecturer_id)
                return Room(**dict(row))
            except asyncpg.UniqueViolationError as e:
                if "join_code" not in str(e):
                    raise
        raise RuntimeError("could not generate a unique join code")

    async def get_join_target(self, raw_code: str) -> Optional[JoinTarget]:
        """Resolves what a student typed. None: unknown code. A target with
        session=None: valid code, nothing live in that room yet."""
        code = normalize_join_code(raw_code)
        if not 6 <= len(code) <= 10:
            return None
        row = await self._pool.fetchrow(
            "SELECT r.id AS room_id, r.institution_id, r.room_code, r.join_code, "
            "       s.id AS session_id, s.lecturer_id, s.source_language, s.target_language, s.started_at "
            "FROM rooms r LEFT JOIN lecture_sessions s ON s.room_id = r.id AND s.status = 'LIVE' "
            "WHERE r.join_code = $1", code)
        if row is None:
            return None
        room = Room(id=row["room_id"], institution_id=row["institution_id"],
                    room_code=row["room_code"], join_code=row["join_code"])
        session = None
        if row["session_id"] is not None:
            session = LiveSession(id=row["session_id"], institution_id=row["institution_id"], room_id=row["room_id"],
                                  lecturer_id=row["lecturer_id"], source_language=row["source_language"],
                                  target_language=row["target_language"], started_at=row["started_at"])
        return JoinTarget(room=room, session=session)

    async def get_usage(self, institution_id: uuid.UUID) -> Usage:
        inst = await self._pool.fetchrow(
            "SELECT monthly_audio_limit_hours, timezone FROM institutions WHERE id = $1", institution_id)
        if inst is None:
            raise RoomNotFound("Institution not found")
        used = await self._pool.fetchval(_USED_SECONDS_SQL, institution_id, inst["timezone"])
        return Usage(used_seconds=used, limit_seconds=inst["monthly_audio_limit_hours"] * 3600)

    # ---- session lifecycle ------------------------------------------

    async def start_session(self, room_id: uuid.UUID, lecturer_id: uuid.UUID,
                            source_language: str, target_language: str) -> LiveSession:
        """Starts a LIVE session if the institution may. One transaction, holding
        a lock on the institution row, so two lectures starting at the same
        moment can't both slip under the quota."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT r.institution_id, i.subscription_status, i.monthly_audio_limit_hours, i.timezone "
                    "FROM rooms r JOIN institutions i ON i.id = r.institution_id "
                    "WHERE r.id = $1 FOR UPDATE OF i", room_id)
                if row is None:
                    raise RoomNotFound("Room not found")
                if row["subscription_status"] not in _STARTABLE_STATUSES:
                    raise InstitutionInactive(f"Subscription is {row['subscription_status'].lower()}")
                allowed = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM users WHERE id = $1 AND institution_id = $2 AND is_active "
                    "               AND role IN ('ADMIN', 'LECTURER') AND email_verified_at IS NOT NULL)",
                    lecturer_id, row["institution_id"])
                if not allowed:
                    raise LecturerNotAllowed("Lecturer is not an active member of this institution")
                used = await conn.fetchval(_USED_SECONDS_SQL, row["institution_id"], row["timezone"])
                limit = row["monthly_audio_limit_hours"] * 3600
                if used >= limit:
                    raise QuotaExceeded(used, limit)
                try:
                    s = await conn.fetchrow(
                        "INSERT INTO lecture_sessions (institution_id, room_id, lecturer_id, source_language, target_language) "
                        "VALUES ($1, $2, $3, $4, $5) RETURNING id, started_at",
                        row["institution_id"], room_id, lecturer_id, source_language, target_language)
                except asyncpg.UniqueViolationError:
                    raise RoomBusy("This room already has a live lecture")
        return LiveSession(id=s["id"], institution_id=row["institution_id"], room_id=room_id, lecturer_id=lecturer_id,
                           source_language=source_language, target_language=target_language, started_at=s["started_at"])

    async def heartbeat(self, session_id: uuid.UUID) -> bool:
        """False means the session is no longer LIVE (e.g. it was reaped)."""
        result = await self._pool.execute(
            "UPDATE lecture_sessions SET last_heartbeat_at = now() WHERE id = $1 AND status = 'LIVE'", session_id)
        return result.endswith(" 1")

    async def complete_session(self, session_id: uuid.UUID, reason: str) -> Optional[int]:
        """Ends a LIVE session and records its billable seconds. Atomic and
        idempotent: only the first call for a session finds it LIVE and gets a
        number back; any later call returns None and changes nothing."""
        return await self._pool.fetchval(
            "UPDATE lecture_sessions SET status = 'COMPLETED', ended_at = GREATEST(now(), started_at), end_reason = $2, "
            "       duration_seconds = FLOOR(EXTRACT(EPOCH FROM GREATEST(now() - started_at, interval '0')))::int "
            "WHERE id = $1 AND status = 'LIVE' RETURNING duration_seconds", session_id, reason)

    async def abort_stale_sessions(self, stale_after_s: float, reason: str = "heartbeat_timeout") -> list[tuple[uuid.UUID, int]]:
        """Marks LIVE sessions whose heartbeat stopped as ABORTED. Their
        duration runs to the last heartbeat, the last moment we know they were
        alive, so a crash neither loses the usage nor bills the downtime.

        ended_at is clamped to never precede started_at (a clock that stepped
        back, a failover). The whole batch is one UPDATE, so without the clamp a
        single inconsistent row would fail the CHECK constraint on every reaper
        cycle and block every other stale session from being settled."""
        rows = await self._pool.fetch(
            "UPDATE lecture_sessions SET status = 'ABORTED', ended_at = GREATEST(last_heartbeat_at, started_at), end_reason = $2, "
            "       duration_seconds = FLOOR(EXTRACT(EPOCH FROM GREATEST(last_heartbeat_at - started_at, interval '0')))::int "
            "WHERE status = 'LIVE' AND last_heartbeat_at < now() - make_interval(secs => $1) "
            "RETURNING id, duration_seconds", float(stale_after_s), reason)
        return [(r["id"], r["duration_seconds"]) for r in rows]
