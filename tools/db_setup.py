#!/usr/bin/env python3
"""
Database setup helper (reads DATABASE_URL and JWT_SECRET from .env).

    python tools/db_setup.py                    apply pending migrations
    python tools/db_setup.py --seed-demo        ...and create a demo school with an admin, a lecturer, a student and a room
    python tools/db_setup.py --token <email>    print a 12-hour access token for an existing account (for the command-line test clients)

The demo accounts have known passwords (printed below). They are for local development only.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import auth  # noqa: E402
from app.config import load_config  # noqa: E402
from app.db import Database  # noqa: E402

DOMAIN = "demo.example"
ROOM_LABEL = "DEMO-101"
DEMO = [("ADMIN", f"admin@{DOMAIN}", "Demo Admin", "DemoAdmin-2026"),
        ("LECTURER", f"lecturer@{DOMAIN}", "Demo Lecturer", "DemoLecturer-2026"),
        ("STUDENT", f"student@{DOMAIN}", "Demo Student", "DemoStudent-2026")]


async def seed_demo(db: Database):
    async with db.pool.acquire() as c:
        inst = await c.fetchval("SELECT id FROM institutions WHERE lower(domain) = $1", DOMAIN)
        if inst is None:
            inst = await c.fetchval(
                "INSERT INTO institutions (name, domain, admin_email, subscription_status, monthly_audio_limit_hours) "
                "VALUES ('Demo University', $1, $2, 'TRIAL', 10) RETURNING id", DOMAIN, f"admin@{DOMAIN}")
        ids = {}
        for role, email, name, password in DEMO:
            pw_hash = auth.hash_password(password)
            uid = await c.fetchval("SELECT id FROM users WHERE lower(email) = $1", email)
            if uid is None:
                uid = await c.fetchval(
                    "INSERT INTO users (institution_id, email, password_hash, full_name, role, email_verified_at) "
                    "VALUES ($1, $2, $3, $4, $5, now()) RETURNING id", inst, email, pw_hash, name, role)
            else:  # re-running the seed resets the demo accounts to a known state
                await c.execute("UPDATE users SET password_hash = $2, email_verified_at = COALESCE(email_verified_at, now()), "
                                "is_active = true WHERE id = $1", uid, pw_hash)
            ids[role] = uid
    room = await db.get_room_by_label(inst, ROOM_LABEL)
    if room is None:
        room = await db.create_room(inst, ROOM_LABEL, "Demo room", ids["LECTURER"])
    return inst, ids, room


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed-demo", action="store_true")
    parser.add_argument("--token", metavar="EMAIL")
    args = parser.parse_args()

    cfg = load_config()
    if not cfg.database_url:
        print("DATABASE_URL is not set. Put it in .env, e.g. DATABASE_URL=postgresql://user:pass@localhost:5432/blb")
        return 1
    db = await Database.connect(cfg.database_url)
    try:
        applied = await db.migrate(ROOT / "migrations")
        print("Migrations applied:", ", ".join(applied) if applied else "none pending")
        if (args.token or args.seed_demo) and not cfg.jwt_secret:
            print("JWT_SECRET is not set in .env")
            return 1
        if args.token:
            row = await db.pool.fetchrow("SELECT id FROM users WHERE lower(email) = lower($1)", args.token)
            user = await db.get_active_user(row["id"]) if row else None
            if user is None:
                print("No active, verified account with that email.")
                return 1
            print(auth.issue_access_token(user.id, user.role, user.institution_id, cfg.jwt_secret, 12 * 3600))
        if args.seed_demo:
            inst, ids, room = await seed_demo(db)
            tok = lambda role: auth.issue_access_token(ids[role], role, inst, cfg.jwt_secret, 12 * 3600)  # noqa: E731
            print(f"\nDemo school ready (domain {DOMAIN}).")
            for role, email, _, password in DEMO:
                print(f"  {role:<9} {email:<24} password: {password}")
            print(f"\n  room label: {room.room_code}     join code: {room.join_code}")
            print("\nTry it with the command-line clients (access tokens valid 12 hours):")
            print(f"  python tools/receive_student_audio.py --code {room.join_code} --token {tok('STUDENT')} --question-audio sample_audio/my_question.wav --ask-after-segments 2")
            print(f"  python tools/send_test_audio.py sample_audio/lecture_sample_id_v2.wav --room {room.room_code} --token {tok('LECTURER')}")
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
