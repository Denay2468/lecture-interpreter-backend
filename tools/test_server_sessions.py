#!/usr/bin/env python3
"""
The real Server against a REAL PostgreSQL: WebSocket handshakes, join-code
lookup, lecture start/quota/busy refusals, heartbeats, billing on disconnect,
and closing students when the lecture ends. The speech pipeline is replaced by
a stub, so no Deepgram/DeepL/ElevenLabs is involved.

Needs TEST_DATABASE_URL (a throwaway schema is created and dropped); skipped otherwise.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import asyncpg  # noqa: E402
import websockets  # noqa: E402

from app import auth  # noqa: E402


def mint_token(user_id, secret, ttl_s=3600, role="LECTURER"):
    return auth.issue_access_token(user_id, role, uuid.uuid4(), secret, ttl_s)
from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402
from app.playback import StudentPlaybackController  # noqa: E402
from app.server import Server  # noqa: E402
from app.session import SessionCoordinator  # noqa: E402
from test_db_layer import Seed  # noqa: E402

DSN = os.getenv("TEST_DATABASE_URL")
SECRET = "test-secret-test-secret-test-secret-0123456789"
PORT, LEGACY_PORT = 8794, 8795
LECTURE = {"type": "mode_changed", "mode": "lecture"}
FAILURES = 0


def check(cond: bool, desc: str) -> None:
    global FAILURES
    print(f"  [{'PASS' if cond else 'FAIL'}] {desc}")
    if not cond:
        FAILURES += 1


class StubCoordinator(SessionCoordinator):
    """The real coordinator minus the speech pipeline."""
    fail_attach = False

    async def attach_teacher(self, session_id):
        if StubCoordinator.fail_attach:
            raise RuntimeError("stub attach failure")
        self._teacher_session_id = session_id
        self._playback = StudentPlaybackController(session_id)
        self._teacher_ingest = object()

    async def detach_teacher(self, session_id):
        self._teacher_ingest = None
        self._playback = None
        self._teacher_session_id = None


async def attempt(uri):
    """Connects; returns (close_code, close_reason, first message). Leaving closes the connection."""
    first = None
    async with websockets.connect(uri) as ws:
        try:
            msg = await asyncio.wait_for(ws.recv(), 3)
            first = json.loads(msg) if isinstance(msg, str) else msg
        except (websockets.ConnectionClosed, asyncio.TimeoutError):
            pass
        return ws.close_code, ws.close_reason, first


async def first_message(ws):
    return json.loads(await asyncio.wait_for(ws.recv(), 3))


async def wait_until(pred, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if await pred():
            return True
        await asyncio.sleep(0.05)
    return False


async def label_of(pool, room_id):
    return await pool.fetchval("SELECT room_code FROM rooms WHERE id = $1", room_id)


async def latest(pool, room_id):
    return await pool.fetchrow("SELECT * FROM lecture_sessions WHERE room_id = $1 ORDER BY started_at DESC LIMIT 1", room_id)


async def run(db: Database) -> None:
    s, pool = Seed(db), db._pool
    server = Server(Config(jwt_secret=SECRET, heartbeat_interval_s=0.2, stale_session_after_s=5.0),
                    db=db, coordinator_factory=StubCoordinator)
    inst = await s.institution(limit_hours=10)
    lec = await s.user(inst)
    room_a, code_a = await s.room(inst)
    room_b, code_b = await s.room(inst)
    label_a = await label_of(pool, room_a)
    token = mint_token(lec, SECRET)
    stu_tok = mint_token(await s.user(inst, role="STUDENT"), SECRET, role="STUDENT")
    base = f"ws://localhost:{PORT}"

    def teacher(label, tok=None, extra=""):
        return f"{base}/ws/teacher?room={label}&token={tok or token}{extra}"

    def student(code, tok=None):
        return f"{base}/ws/student?code={code}&token={tok or stu_tok}"

    async with websockets.serve(server._router, "localhost", PORT):
        print("\ntest_teacher_handshake_refusals")
        async def refused(url, code, why, contains=None):
            got, reason, _ = await attempt(url)
            check(got == code and (contains is None or contains in reason.lower()), f"{why} -> {code} (got {got}: {reason!r})")

        await refused(teacher(label_a, tok="garbage"), 4401, "a garbage token is refused")
        await refused(teacher(label_a, tok=mint_token(lec, SECRET, ttl_s=-30)), 4401, "an expired token is refused")
        await refused(teacher(label_a, tok=mint_token(uuid.uuid4(), SECRET)), 4401, "a token for an unknown user is refused")
        await refused(teacher(label_a, tok=mint_token(await s.user(inst, active=False), SECRET)), 4401, "a deactivated lecturer is refused")
        await refused(teacher("NO-SUCH-ROOM"), 4404, "an unknown room label is refused")
        other = await s.institution()
        await refused(teacher(label_a, tok=mint_token(await s.user(other), SECRET)), 4404, "another institution's lecturer can't reach this room")
        await refused(teacher(label_a, extra="&source=en&target=id"), 4400, "an unsupported language pair is refused")
        susp = await s.institution(status="SUSPENDED")
        await refused(teacher(await label_of(pool, (await s.room(susp))[0]), tok=mint_token(await s.user(susp), SECRET)),
                      4403, "a suspended institution is refused", "subscription")
        zero = await s.institution(limit_hours=0)
        await refused(teacher(await label_of(pool, (await s.room(zero))[0]), tok=mint_token(await s.user(zero), SECRET)),
                      4403, "an exhausted quota is refused with a readable reason", "limit")
        check(await pool.fetchval("SELECT count(*) FROM lecture_sessions WHERE room_id = $1", room_a) == 0,
              "none of the refusals left a session row behind")

        print("\ntest_roles_are_enforced_on_both_routes")
        other_stu = mint_token(await s.user(other, role="STUDENT"), SECRET, role="STUDENT")
        await refused(teacher(label_a, tok=stu_tok), 4403, "a student can't start a lecture", "lecturer")
        await refused(f"{base}/ws/student?code={code_a}&token={token}", 4403, "a lecturer can't join as a student", "student")
        await refused(f"{base}/ws/student?code={code_a}", 4401, "joining without signing in is refused")
        await refused(student(code_a, tok="garbage"), 4401, "a garbage token is refused")
        await refused(student(code_a, tok=mint_token(uuid.uuid4(), SECRET, role="STUDENT")), 4401, "a token for an account that doesn't exist is refused")
        await refused(student(code_a, tok=other_stu), 4403, "a student from another school can't join this room", "different school")
        await refused(student("ZZZZZZ", tok=mint_token(lec, SECRET, role="ADMIN")), 4401, "a token claiming a role the account doesn't have is refused")

        print("\ntest_lecture_lifecycle_over_websockets")
        async with websockets.connect(teacher(label_a)) as t:
            check(await first_message(t) == LECTURE, "the teacher is told the current mode on connect")
            row = await latest(pool, room_a)
            check(row["status"] == "LIVE" and (row["source_language"], row["target_language"]) == ("id", "en") and row["lecturer_id"] == lec,
                  "a LIVE session is recorded with the id -> en pair and the right lecturer")
            sid = row["id"]

            async def beat():
                return await pool.fetchval("SELECT last_heartbeat_at > started_at + interval '0.3 seconds' FROM lecture_sessions WHERE id = $1", sid)
            check(await wait_until(beat), "heartbeats advance while the lecture runs")
            got, reason, _ = await attempt(teacher(label_a))
            check(got == 4409, f"a second teacher in the same room is refused (got {got}: {reason!r})")
            check((await latest(pool, room_a))["id"] == sid and (await latest(pool, room_a))["status"] == "LIVE", "the running lecture is unaffected")

            print("\ntest_student_join_by_room_code")
            got, reason, _ = await attempt(student("ZZZZZZ"))
            check(got == 4404, f"an unknown code is refused (got {got}: {reason!r})")
            got, reason, _ = await attempt(student(code_b))
            check(got == 4412, f"a valid code with no live lecture is told to retry (got {got}: {reason!r})")
            async with websockets.connect(student(code_a.lower())) as st:
                check(await first_message(st) == LECTURE, "a student joins with a lowercase code and is told the mode")
                got, reason, _ = await attempt(student(code_a))
                check(got == 4409, f"a second student is refused rather than stealing segments (got {got}: {reason!r})")
                await t.close()
                try:
                    await asyncio.wait_for(st.recv(), 5)
                except websockets.ConnectionClosed:
                    pass
                check(st.close_code == 4413, f"the student is told the lecture ended (got {st.close_code})")

        async def done():
            return (await latest(pool, room_a))["status"] == "COMPLETED"
        check(await wait_until(done), "the session is COMPLETED after the teacher disconnects")
        row = await latest(pool, room_a)
        check(row["end_reason"] == "teacher_disconnected" and row["duration_seconds"] is not None and row["duration_seconds"] >= 0,
              f"the reason and billable seconds are recorded ({row['end_reason']}, {row['duration_seconds']}s)")
        check((await db.get_usage(inst)).used_seconds >= row["duration_seconds"], "the lecture counts toward the monthly usage")
        got, _, _ = await attempt(student(code_a))
        check(got == 4412, "after the lecture ends, the code reports nothing live")

        print("\ntest_room_is_reusable_and_failures_are_settled")
        async with websockets.connect(teacher(label_a)) as t2:
            await first_message(t2)
            check((await latest(pool, room_a))["id"] != sid, "a new lecture in the same room is a new session")
        await wait_until(done)
        StubCoordinator.fail_attach = True
        got, reason, _ = await attempt(teacher(label_a))
        StubCoordinator.fail_attach = False
        check(got == 1011 and "stub attach failure" in reason, f"a speech-pipeline failure is reported to the teacher (got {got}: {reason!r})")
        await wait_until(done)
        row = await latest(pool, room_a)
        check(row["status"] == "COMPLETED" and row["end_reason"] == "attach_failed", "the failed start is settled, not left LIVE")
        async with websockets.connect(teacher(label_a)) as t3:
            check(await first_message(t3) == LECTURE, "and the room can be used again immediately")

    print("\ntest_single_session_mode_still_works")
    legacy = Server(Config(), coordinator_factory=StubCoordinator)
    async with websockets.serve(legacy._router, "localhost", LEGACY_PORT):
        lb = f"ws://localhost:{LEGACY_PORT}"
        async with websockets.connect(f"{lb}/ws/teacher?session_id=abc") as lt:
            check(await first_message(lt) == LECTURE, "a teacher connects without a database")
            got, _, _ = await attempt(f"{lb}/ws/teacher?session_id=def")
            check(got == 4409, "a second teacher is refused")
            async with websockets.connect(f"{lb}/ws/student") as ls:
                check(await first_message(ls) == LECTURE, "a student connects without a code")
                got, _, _ = await attempt(f"{lb}/ws/student")
                check(got == 4409, "a second student is refused")
                await lt.close()
                try:
                    await asyncio.wait_for(ls.recv(), 0.8)
                    still_open = False
                except asyncio.TimeoutError:
                    still_open = True
                except websockets.ConnectionClosed:
                    still_open = False
                check(still_open, "in single-session mode the student stays connected after the teacher leaves (unchanged)")


async def main() -> int:
    if not DSN:
        print("SKIPPED: set TEST_DATABASE_URL to run the server tests against a real PostgreSQL.")
        return 0
    schema = "t_" + uuid.uuid4().hex[:10]
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    db = await Database.connect(DSN, search_path=schema)
    try:
        await db.migrate(ROOT / "migrations")
        await run(db)
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
