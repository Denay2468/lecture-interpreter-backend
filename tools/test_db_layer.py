#!/usr/bin/env python3
"""
Tests for the schema, the asyncpg layer and the session lifecycle, run against
a REAL PostgreSQL. Needs TEST_DATABASE_URL (any database; the test creates its
own throwaway schema and drops it afterwards, so it never touches your tables).
Without that variable it prints SKIPPED and exits 0.

    TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/postgres python tools/test_db_layer.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402

from app.db import (Database, InstitutionInactive, LecturerNotAllowed, QuotaExceeded,  # noqa: E402
                    RoomBusy, RoomNotFound, normalize_join_code)
from app.lifecycle import SessionLifecycle  # noqa: E402

DSN = os.getenv("TEST_DATABASE_URL")
MIGRATIONS = ROOT / "migrations"
FAILURES = 0
Check, Unique, FK = (asyncpg.exceptions.CheckViolationError, asyncpg.exceptions.UniqueViolationError,
                     asyncpg.exceptions.ForeignKeyViolationError)


def check(cond: bool, desc: str) -> None:
    global FAILURES
    print(f"  [{'PASS' if cond else 'FAIL'}] {desc}")
    if not cond:
        FAILURES += 1


async def raises(exc, coro) -> bool:
    try:
        await coro
    except exc:
        return True
    except Exception as e:  # noqa: BLE001
        print(f"     (wrong exception: {type(e).__name__}: {e})")
        return False
    return False


class Seed:
    def __init__(self, db: Database) -> None:
        self.db, self.pool = db, db._pool

    async def institution(self, limit_hours=10, status="ACTIVE", tz="Asia/Jakarta"):
        t = uuid.uuid4().hex[:8]
        return await self.pool.fetchval(
            "INSERT INTO institutions (name, domain, admin_email, subscription_status, monthly_audio_limit_hours, timezone) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id", f"Inst {t}", f"{t}.example", f"a@{t}.example", status, limit_hours, tz)

    async def user(self, inst, role="LECTURER", active=True, verified=True):
        t = uuid.uuid4().hex[:8]
        domain = await self.pool.fetchval("SELECT domain FROM institutions WHERE id = $1", inst)
        return await self.pool.fetchval(
            "INSERT INTO users (institution_id, email, password_hash, full_name, role, is_active, email_verified_at) "
            "VALUES ($1, $2, '!', 'Test User', $3, $4, CASE WHEN $5::boolean THEN now() END) RETURNING id",
            inst, f"{t}@{domain}", role, active, verified)

    async def room(self, inst):
        r = await self.db.create_room(inst, "R-" + uuid.uuid4().hex[:6])
        return r.id, r.join_code

    async def session(self, inst, room, lec, status="LIVE", started_at=None, duration=None, src="id", tgt="en"):
        started_at = started_at or datetime.now(timezone.utc)
        live = status == "LIVE"
        ended = None if live else started_at + timedelta(seconds=duration or 0)
        return await self.pool.fetchval(
            "INSERT INTO lecture_sessions (institution_id, room_id, lecturer_id, source_language, target_language, status, "
            "started_at, last_heartbeat_at, ended_at, duration_seconds, end_reason) VALUES ($1,$2,$3,$4,$5,$6,$7,$7,$8,$9,$10) RETURNING id",
            inst, room, lec, src, tgt, status, started_at, ended, None if live else (duration or 0), None if live else "seed")

    async def status(self, session_id):
        return await self.pool.fetchrow("SELECT status, end_reason, duration_seconds, ended_at, last_heartbeat_at, started_at "
                                        "FROM lecture_sessions WHERE id = $1", session_id)

    async def month_start(self, tz="Asia/Jakarta"):
        return await self.pool.fetchval("SELECT date_trunc('month', now() AT TIME ZONE $1::text) AT TIME ZONE $1::text", tz)


async def test_migrations(db: Database) -> None:
    print("\ntest_migrations_are_idempotent")
    check(await db.migrate(MIGRATIONS) == [], "a second run applies nothing")
    n = await db._pool.fetchval("SELECT count(*) FROM schema_migrations")
    expected = len(list(MIGRATIONS.glob("*.sql")))
    check(n == expected, f"one version recorded per migration file (got {n}, expected {expected})")


async def test_constraints(s: Seed) -> None:
    print("\ntest_schema_constraints")
    a, b = await s.institution(), await s.institution()
    lec_a, lec_b = await s.user(a), await s.user(b)
    room_a, _ = await s.room(a)
    check(await raises(FK, s.session(a, room_a, lec_b)), "a session can't pair a room from one institution with a lecturer from another")
    check(await raises(FK, s.pool.execute("INSERT INTO rooms (institution_id, room_code, join_code, assigned_lecturer_id) VALUES ($1,'X','ABCDEF',$2)", a, lec_b)),
          "a room's assigned lecturer must belong to the same institution")
    check(await raises(Check, s.pool.execute("INSERT INTO lecture_sessions (institution_id, room_id, lecturer_id, status, ended_at) VALUES ($1,$2,$3,'LIVE',now())", a, room_a, lec_a)),
          "a LIVE session can't have an end time")
    check(await raises(Check, s.pool.execute("INSERT INTO lecture_sessions (institution_id, room_id, lecturer_id, status, ended_at) VALUES ($1,$2,$3,'COMPLETED',now())", a, room_a, lec_a)),
          "an ended session must record duration and reason")
    check(await raises(Check, s.session(a, room_a, lec_a, src="id", tgt="id")), "source and target language must differ")
    check(await raises(Check, s.session(a, room_a, lec_a, src="EN", tgt="id")), "language codes must be lowercase ISO codes")
    await s.session(a, room_a, lec_a, status="COMPLETED", duration=60)
    await s.session(a, room_a, lec_a)
    check(True, "a COMPLETED and a LIVE session can share a room")
    check(await raises(Unique, s.session(a, room_a, lec_a)), "a second LIVE session in the same room is rejected by the database")
    check(await raises(Check, s.pool.execute("INSERT INTO rooms (institution_id, room_code, join_code) VALUES ($1,'Y','abcdef')", a)), "join codes must be uppercase")
    check(await raises(Check, s.pool.execute("INSERT INTO rooms (institution_id, room_code, join_code) VALUES ($1,'Y','ABCDE0')", a)), "join codes can't contain ambiguous characters")
    _, code = await s.room(a)
    check(await raises(Unique, s.pool.execute("INSERT INTO rooms (institution_id, room_code, join_code) VALUES ($1,'Z',$2)", b, code)), "join codes are unique across institutions")


async def test_start_rules(s: Seed) -> None:
    print("\ntest_start_session_rules")
    db = s.db
    inst = await s.institution()
    lec, room = await s.user(inst), (await s.room(inst))[0]
    live = await db.start_session(room, lec, "id", "en")
    row = await s.status(live.id)
    check(row["status"] == "LIVE" and live.source_language == "id" and live.target_language == "en", "starts LIVE with the languages recorded")
    check(await raises(RoomBusy, db.start_session(room, lec, "id", "en")), "a room can't start a second lecture while one is live")
    other_room = (await s.room(inst))[0]
    await db.start_session(other_room, lec, "id", "en")
    check(True, "a different room in the same institution can")
    check(await raises(InstitutionInactive, db.start_session((await s.room(sus := await s.institution(status="SUSPENDED")))[0], await s.user(sus), "id", "en")),
          "a suspended institution can't start lectures")
    check(await raises(LecturerNotAllowed, db.start_session((await s.room(inst))[0], await s.user(await s.institution()), "id", "en")),
          "a lecturer from another institution can't")
    check(await raises(LecturerNotAllowed, db.start_session((await s.room(inst))[0], await s.user(inst, active=False), "id", "en")),
          "a deactivated lecturer can't")
    check(await raises(LecturerNotAllowed, db.start_session((await s.room(inst))[0], await s.user(inst, verified=False), "id", "en")),
          "an account whose email isn't verified can't")
    check(await raises(LecturerNotAllowed, db.start_session((await s.room(inst))[0], await s.user(inst, role="STUDENT"), "id", "en")),
          "a student can't start a lecture")
    check(await raises(RoomNotFound, db.start_session(uuid.uuid4(), lec, "id", "en")), "an unknown room is reported as such")


async def test_quota(s: Seed) -> None:
    print("\ntest_monthly_quota")
    db = s.db
    inst = await s.institution(limit_hours=1)
    lec, room = await s.user(inst), (await s.room(inst))[0]
    ms = await s.month_start()
    await s.session(inst, room, lec, status="COMPLETED", started_at=ms - timedelta(days=3), duration=7200)
    await s.session(inst, room, lec, status="COMPLETED", started_at=ms, duration=3599)
    usage = await db.get_usage(inst)
    check(usage.used_seconds == 3599 and usage.limit_seconds == 3600, f"last month's usage doesn't count (used={usage.used_seconds}, limit={usage.limit_seconds})")
    live = await db.start_session(room, lec, "id", "en")
    check(True, "starting is allowed one second under the limit")
    await db.complete_session(live.id, "test")
    await s.session(inst, room, lec, status="COMPLETED", started_at=ms, duration=2)
    try:
        await db.start_session(room, lec, "id", "en")
        check(False, "starting is refused once the limit is reached")
    except QuotaExceeded as e:
        check(e.used_seconds >= 3600 and e.limit_seconds == 3600, f"starting is refused at the limit ({e})")
    zero = await s.institution(limit_hours=0)
    check(await raises(QuotaExceeded, db.start_session((await s.room(zero))[0], await s.user(zero), "id", "en")), "a zero-hour plan can't start")
    run = await s.institution(limit_hours=1000)
    lr, rr = await s.user(run), (await s.room(run))[0]
    ms2 = await s.month_start()
    await s.session(run, rr, lr, started_at=ms2)
    elapsed = await s.pool.fetchval("SELECT FLOOR(EXTRACT(EPOCH FROM (now() - $1)))::bigint", ms2)
    used = (await db.get_usage(run)).used_seconds
    check(abs(used - elapsed) <= 2, f"a running session counts for its elapsed time (used={used}, elapsed={elapsed})")


async def test_complete(s: Seed) -> None:
    print("\ntest_complete_is_exact_and_idempotent")
    db = s.db
    inst = await s.institution()
    lec, room = await s.user(inst), (await s.room(inst))[0]
    live = await db.start_session(room, lec, "id", "en")
    await s.pool.execute("UPDATE lecture_sessions SET started_at = now() - interval '90 seconds' WHERE id = $1", live.id)
    d = await db.complete_session(live.id, "lecturer_ended")
    row = await s.status(live.id)
    check(d is not None and 90 <= d <= 92, f"duration is measured by the database clock (got {d}s)")
    check(row["status"] == "COMPLETED" and row["end_reason"] == "lecturer_ended" and row["duration_seconds"] == d, "status, reason and duration recorded together")
    check(await db.complete_session(live.id, "again") is None, "a second completion changes nothing and returns None")
    check((await s.status(live.id))["end_reason"] == "lecturer_ended", "the first reason is kept")
    check(await db.heartbeat(live.id) is False, "heartbeat on an ended session reports False")
    check(await db.start_session(room, lec, "id", "en") is not None, "the room is free to start again")


async def test_stale(s: Seed) -> None:
    print("\ntest_stale_sessions_are_aborted_not_lost")
    db = s.db
    inst = await s.institution()
    lec = await s.user(inst)
    r1, r2 = (await s.room(inst))[0], (await s.room(inst))[0]
    stale = await s.session(inst, r1, lec, started_at=datetime.now(timezone.utc) - timedelta(minutes=20))
    await s.pool.execute("UPDATE lecture_sessions SET last_heartbeat_at = now() - interval '10 minutes' WHERE id = $1", stale)
    fresh = await s.session(inst, r2, lec)
    aborted = dict(await db.abort_stale_sessions(60))
    row = await s.status(stale)
    check(stale in aborted and 599 <= aborted[stale] <= 601, f"duration runs to the last heartbeat (got {aborted.get(stale)}s), not to 'now'")
    check(row["status"] == "ABORTED" and row["end_reason"] == "heartbeat_timeout" and row["ended_at"] == row["last_heartbeat_at"], "marked ABORTED at its last heartbeat")
    check((await s.status(fresh))["status"] == "LIVE", "a session with a recent heartbeat is left alone")
    check(await db.heartbeat(fresh) is True, "heartbeat on a live session reports True")


async def test_join(s: Seed) -> None:
    print("\ntest_join_code_lookup")
    db = s.db
    inst = await s.institution()
    lec = await s.user(inst)
    room, code = await s.room(inst)
    t = await db.get_join_target(code)
    check(t is not None and t.room.id == room and t.session is None, "a valid code with nothing live resolves to the room, no session")
    live = await db.start_session(room, lec, "id", "en")
    t = await db.get_join_target(code.lower()[:3] + " - " + code.lower()[3:])
    check(t is not None and t.session is not None and t.session.id == live.id, "lowercase / spaced / dashed input resolves to the live session")
    check(await db.get_join_target("ZZZZZZ") is None, "an unknown code resolves to nothing")
    check(await db.get_join_target("abc") is None, "a malformed code is rejected without a query")
    check(normalize_join_code(" k7p-2qx ") == "K7P2QX", "normalisation strips punctuation and case")


async def test_concurrency(s: Seed) -> None:
    print("\ntest_concurrent_starts")
    db = s.db
    inst = await s.institution()
    lec = await s.user(inst)
    room = (await s.room(inst))[0]
    results = await asyncio.gather(*(db.start_session(room, lec, "id", "en") for _ in range(4)), return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    busy = [r for r in results if isinstance(r, RoomBusy)]
    check(len(ok) == 1 and len(busy) == 3, f"four simultaneous starts in one room: exactly one wins (ok={len(ok)}, busy={len(busy)})")
    rooms = [(await s.room(inst))[0] for _ in range(3)]
    results = await asyncio.gather(*(db.start_session(r, lec, "id", "en") for r in rooms), return_exceptions=True)
    check(all(not isinstance(r, Exception) for r in results), "simultaneous starts in different rooms all succeed")


async def test_lifecycle(s: Seed) -> None:
    print("\ntest_lifecycle_manager")
    db = s.db
    lc = SessionLifecycle(db, heartbeat_interval_s=0.1, stale_after_s=0.4)
    inst = await s.institution()
    lec = await s.user(inst)
    room = (await s.room(inst))[0]
    live = await lc.start_session(room, lec, "id", "en")
    await asyncio.sleep(0.45)
    row = await s.status(live.id)
    check((row["last_heartbeat_at"] - row["started_at"]).total_seconds() >= 0.25, "heartbeats keep advancing while the lecture runs")
    d = await lc.end_session(live.id, "lecturer_ended")
    check(d is not None and d >= 0 and (await s.status(live.id))["status"] == "COMPLETED", f"end_session records the billable seconds ({d}s)")
    check(await lc.end_session(live.id, "lecturer_ended") is None, "ending twice is harmless")
    stale_room = (await s.room(inst))[0]
    stale = await s.session(inst, stale_room, lec, started_at=datetime.now(timezone.utc) - timedelta(hours=2))
    await s.pool.execute("UPDATE lecture_sessions SET last_heartbeat_at = now() - interval '1 hour' WHERE id = $1", stale)
    lc.start_reaper()
    await asyncio.sleep(0.35)
    check((await s.status(stale))["status"] == "ABORTED", "the background reaper settles a crashed session")
    other = await lc.start_session((await s.room(inst))[0], lec, "id", "en")
    await lc.stop()
    row = await s.status(other.id)
    check(row["status"] == "COMPLETED" and row["end_reason"] == "server_shutdown", "stop() ends running sessions cleanly")
    leftover = await s.session(inst, (await s.room(inst))[0], lec)
    check(await lc.recover_on_startup() >= 1 and (await s.status(leftover))["end_reason"] == "server_restart", "recover_on_startup settles sessions a dead process left LIVE")


async def test_abort_with_clock_anomaly(s: Seed) -> None:
    print("\ntest_one_inconsistent_row_cannot_block_the_reaper")
    db = s.db
    inst = await s.institution()
    lec = await s.user(inst)
    r1, r2, r3 = [(await s.room(inst))[0] for _ in range(3)]
    odd = await s.session(inst, r1, lec)  # a heartbeat that predates the start: what a stepped-back clock leaves behind
    await s.pool.execute("UPDATE lecture_sessions SET last_heartbeat_at = now() - interval '1 hour' WHERE id = $1", odd)
    normal = await s.session(inst, r2, lec, started_at=datetime.now(timezone.utc) - timedelta(minutes=20))
    await s.pool.execute("UPDATE lecture_sessions SET last_heartbeat_at = now() - interval '10 minutes' WHERE id = $1", normal)
    aborted = dict(await db.abort_stale_sessions(60))
    row = await s.status(odd)
    check(aborted.get(odd) == 0, f"the inconsistent row is settled at 0 seconds instead of raising (got {aborted.get(odd)})")
    check(row["status"] == "ABORTED" and row["ended_at"] == row["started_at"], "ended_at is clamped to started_at, never before it")
    check(normal in aborted and 599 <= aborted[normal] <= 601, "the other stale session in the same batch is still settled, still to its last heartbeat")
    future = await s.session(inst, r3, lec, started_at=datetime.now(timezone.utc) + timedelta(minutes=5))  # clock stepped back after the start
    check(await db.complete_session(future, "test") == 0, "complete_session has the same guard (0 seconds, no constraint error)")


async def main() -> int:
    if not DSN:
        print("SKIPPED: set TEST_DATABASE_URL to run the database tests against a real PostgreSQL.")
        return 0
    schema = "t_" + uuid.uuid4().hex[:10]
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    db = await Database.connect(DSN, search_path=schema)
    try:
        await db.migrate(MIGRATIONS)
        s = Seed(db)
        await test_migrations(db)
        await test_constraints(s)
        await test_start_rules(s)
        await test_quota(s)
        await test_complete(s)
        await test_stale(s)
        await test_abort_with_clock_anomaly(s)
        await test_join(s)
        await test_concurrency(s)
        await test_lifecycle(s)
    finally:
        await db.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
    print()
    if FAILURES:
        print(f"{FAILURES} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
