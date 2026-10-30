#!/usr/bin/env python3
"""
The HTTP API end to end (real FastAPI app, real PostgreSQL, requests sent through httpx): sign-up with a university
email, invites, activation, sign-in, refresh rotation, the role guards, tenant isolation, rate limiting, password
reset. Needs TEST_DATABASE_URL; skipped otherwise.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import asyncpg  # noqa: E402
import httpx  # noqa: E402

from app import auth  # noqa: E402
from app.accounts import Accounts  # noqa: E402
from app.api import Limiters, create_app  # noqa: E402
from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402
from app.ratelimit import RateLimiter  # noqa: E402
from test_accounts_db import CapturingMailer  # noqa: E402

DSN = os.getenv("TEST_DATABASE_URL")
SECRET = "test-secret-test-secret-test-secret-0123456789"
PW = "correct horse battery"
FAILURES = 0


def check(cond: bool, desc: str) -> None:
    global FAILURES
    print(f"  [{'PASS' if cond else 'FAIL'}] {desc}")
    if not cond:
        FAILURES += 1


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def run(db: Database) -> None:
    mailer = CapturingMailer()
    cfg = Config(jwt_secret=SECRET)
    acc = Accounts(db, SECRET, mailer, code_ttl_s=900, refresh_ttl_s=3600)
    app = create_app(cfg, db, acc)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        async def activate(email, password=PW):
            r = await c.post("/auth/activate", json={"email": email, "code": mailer.code(email), "password": password})
            return r

        print("\ntest_school_setup_and_admin")
        check((await c.get("/health")).json() == {"status": "ok"}, "the API is up")
        dom = f"u{uuid.uuid4().hex[:8]}.example"
        inst, _ = await acc.create_institution("Test University", dom, f"admin@{dom}", "Ada Admin")
        await acc.add_domain(inst, f"student.{dom}", allow_staff=False, allow_students=True)
        r = await activate(f"admin@{dom}", "short")
        check(r.status_code == 422 and "at least" in r.json()["detail"], "a weak password is refused with a clear message")
        r = await c.post("/auth/activate", json={"email": f"admin@{dom}", "code": "000000", "password": PW})
        check(r.status_code == 400, "a wrong code is refused")
        r = await activate(f"admin@{dom}")
        check(r.status_code == 200 and r.json()["user"]["role"] == "ADMIN", "the admin activates with the emailed code and is signed in")
        admin_tok = r.json()["access_token"]
        me = (await c.get("/me", headers=bearer(admin_tok))).json()
        check(me["email"] == f"admin@{dom}" and me["institution_name"] == "Test University", "/me returns the signed-in account and school")
        r = await c.post("/auth/login", json={"email": f"admin@{dom}", "password": "wrong password!!"})
        check(r.status_code == 401, "a wrong password is a generic 401")
        r = await c.post("/auth/login", json={"email": "nobody@" + dom, "password": PW})
        check(r.status_code == 401 and r.json() == (await c.post("/auth/login", json={"email": f"admin@{dom}", "password": "wrong password!!"})).json(),
              "an unknown email gets exactly the same answer")
        r = await c.post("/auth/login", json={"email": "not-an-email", "password": PW})
        check(r.status_code == 422 and "valid email" in r.json()["detail"], "a malformed email gets a friendly 422")

        print("\ntest_role_guards")
        check((await c.get("/admin/lecturers")).status_code == 401, "no token -> 401")
        check((await c.get("/admin/lecturers", headers=bearer("garbage"))).status_code == 401, "a garbage token -> 401")
        refresh_as_bearer = (await c.post("/auth/login", json={"email": f"admin@{dom}", "password": PW})).json()["refresh_token"]
        check((await c.get("/me", headers=bearer(refresh_as_bearer))).status_code == 401, "a refresh token can't be used as an access token")
        r = await c.post("/admin/lecturers", json={"email": f"teacher@{dom}", "full_name": "  Tara   Teacher "}, headers=bearer(admin_tok))
        check(r.status_code == 201 and r.json()["full_name"] == "Tara Teacher" and r.json()["email_verified_at"] is None, "the admin invites a lecturer (name tidied)")
        lec_id = r.json()["id"]
        check((await c.post("/admin/lecturers", json={"email": f"teacher@{dom}", "full_name": "X"}, headers=bearer(admin_tok))).status_code == 409, "inviting the same address again -> 409")
        r = await c.post("/admin/lecturers", json={"email": f"x@student.{dom}", "full_name": "X"}, headers=bearer(admin_tok))
        check(r.status_code == 422 and "staff email" in r.json()["detail"], "inviting on a student-only domain -> 422 naming the allowed domains")
        r = await c.post("/admin/lecturers", json={"email": "x@gmail.com", "full_name": "X"}, headers=bearer(admin_tok))
        check(r.status_code == 422, "inviting a non-university address -> 422")
        r = await activate(f"teacher@{dom}")
        lec_tok = r.json()["access_token"]
        check(r.status_code == 200 and r.json()["user"]["role"] == "LECTURER", "the lecturer activates with their invite code")
        check((await c.get("/admin/lecturers", headers=bearer(lec_tok))).status_code == 403, "a lecturer can't use admin routes")
        check((await c.post("/admin/lecturers", json={"email": f"y@{dom}", "full_name": "Y"}, headers=bearer(lec_tok))).status_code == 403, "a lecturer can't invite lecturers")
        forged = auth.issue_access_token(lec_id, "ADMIN", inst, SECRET)
        check((await c.get("/admin/lecturers", headers=bearer(forged))).status_code == 401, "a validly signed token claiming a role the account doesn't have is rejected")

        print("\ntest_lecturer_rooms")
        r = await c.post("/rooms", json={"room_code": "CS-302", "room_name": "Algorithms"}, headers=bearer(lec_tok))
        check(r.status_code == 201 and len(r.json()["join_code"]) == 6, "a lecturer creates a room and gets its join code")
        room = r.json()
        check((await c.post("/rooms", json={"room_code": "cs-302"}, headers=bearer(lec_tok))).status_code == 409, "a duplicate label -> 409")
        rooms = (await c.get("/rooms", headers=bearer(lec_tok))).json()
        check(len(rooms) == 1 and rooms[0]["mine"] is True and rooms[0]["is_live"] is False, "the room list marks which are theirs and which are live")
        check((await c.patch(f"/rooms/{room['id']}", json={"room_name": "Algo II"}, headers=bearer(lec_tok))).json()["room_name"] == "Algo II", "the owner can rename it")
        new_code = (await c.post(f"/rooms/{room['id']}/rotate-code", headers=bearer(lec_tok))).json()["join_code"]
        check(new_code != room["join_code"], "the owner can rotate its join code")
        # a second lecturer
        await c.post("/admin/lecturers", json={"email": f"two@{dom}", "full_name": "Two"}, headers=bearer(admin_tok))
        tok2 = (await activate(f"two@{dom}")).json()["access_token"]
        check((await c.patch(f"/rooms/{room['id']}", json={"room_name": "Mine now"}, headers=bearer(tok2))).status_code == 403, "another lecturer can't change it")
        check((await c.patch(f"/rooms/{room['id']}", json={"room_name": "By admin"}, headers=bearer(admin_tok))).status_code == 200, "but the school admin can")
        room["join_code"] = new_code

        print("\ntest_student_signup_and_join")
        r = await c.post("/auth/register", json={"email": "kim@gmail.com", "full_name": "Kim"})
        check(r.status_code == 422 and "university email" in r.json()["detail"], "signing up with a non-university email is refused")
        r = await c.post("/auth/register", json={"email": f"kim@{dom}", "full_name": "Kim"})
        check(r.status_code == 202, "signing up with the school's email is accepted")
        r2 = await c.post("/auth/register", json={"email": f"kim@{dom}", "full_name": "Kim"})
        check(r2.json() == r.json(), "the answer is the same whether or not the address is already registered")
        kim = f"kim@student.{dom}"
        check((await c.post("/auth/register", json={"email": kim, "full_name": "Kim Student"})).status_code == 202, "a student-domain address is accepted too")
        # the first sign-up (kim@dom) was on the primary domain, which allows students; use the student-domain account
        r = await activate(kim)
        stu_tok = r.json()["access_token"]
        check(r.status_code == 200 and r.json()["user"]["role"] == "STUDENT", "the student activates and is signed in")
        info = (await c.get(f"/join/{room['join_code'].lower()}", headers=bearer(stu_tok))).json()
        check(info == {"room_code": "CS-302", "room_name": "By admin", "is_live": False}, "a student can look up a room code (case-insensitively)")
        check((await c.get("/join/ZZZZZZ", headers=bearer(stu_tok))).status_code == 404, "an unknown code -> 404")
        check((await c.get("/rooms", headers=bearer(stu_tok))).status_code == 403, "a student can't list or manage rooms")
        check((await c.get("/admin/usage", headers=bearer(stu_tok))).status_code == 403, "a student can't see admin data")
        check((await c.get(f"/join/{room['join_code']}")).status_code == 401, "joining info needs sign-in")

        print("\ntest_tenant_isolation")
        odom = f"o{uuid.uuid4().hex[:8]}.example"
        oinst, _ = await acc.create_institution("Other University", odom, f"admin@{odom}", "Other Admin")
        o_tok = (await activate(f"admin@{odom}")).json()["access_token"]
        check((await c.get("/admin/lecturers", headers=bearer(o_tok))).json() == [], "another school's admin sees none of our lecturers")
        check((await c.patch(f"/admin/lecturers/{lec_id}", json={"is_active": False}, headers=bearer(o_tok))).status_code == 404, "...can't deactivate ours (404)")
        check((await c.post(f"/admin/lecturers/{lec_id}/resend-invite", headers=bearer(o_tok))).status_code == 404, "...or re-invite them")
        check((await c.patch(f"/rooms/{room['id']}", json={"room_name": "hijack"}, headers=bearer(o_tok))).status_code == 404, "...or touch our rooms")
        check((await c.get("/rooms", headers=bearer(o_tok))).json() == [], "room lists are per school")
        await c.post("/admin/lecturers", json={"email": f"ot@{odom}", "full_name": "Other Teacher"}, headers=bearer(o_tok))
        o_lec = (await activate(f"ot@{odom}")).json()["access_token"]
        o_room = (await c.post("/rooms", json={"room_code": "X-1"}, headers=bearer(o_lec))).json()
        r = await c.get(f"/join/{o_room['join_code']}", headers=bearer(stu_tok))
        check(r.status_code == 403 and "different school" in r.json()["detail"], "a student can't join another school's room")
        await db.pool.execute("INSERT INTO lecture_sessions (institution_id, room_id, lecturer_id, status, ended_at, duration_seconds, end_reason) "
                              "SELECT $1, $2, $3, 'COMPLETED', now(), 1800, 'test'", inst, uuid.UUID(room["id"]), uuid.UUID(lec_id))
        mine = (await c.get("/admin/sessions", headers=bearer(admin_tok))).json()
        theirs = (await c.get("/admin/sessions", headers=bearer(o_tok))).json()
        check(len(mine) == 1 and mine[0]["duration_seconds"] == 1800 and mine[0]["lecturer_name"] == "Tara Teacher" and theirs == [], "the dashboard's session list is per school")
        usage = (await c.get("/admin/usage", headers=bearer(admin_tok))).json()
        check(usage["used_seconds"] >= 1800 and usage["limit_seconds"] == 36000, "usage counts the lecture against the monthly limit")
        check((await c.get("/admin/email-domains", headers=bearer(admin_tok))).json()[0]["domain"] in (dom, f"student.{dom}"), "the admin can see (not edit) the school's email domains")

        print("\ntest_sessions_over_http")
        login = (await c.post("/auth/login", json={"email": f"teacher@{dom}", "password": PW})).json()
        r = await c.post("/auth/refresh", json={"refresh_token": login["refresh_token"]})
        check(r.status_code == 200 and r.json()["refresh_token"] != login["refresh_token"], "a refresh token is exchanged for new tokens")
        check((await c.post("/auth/refresh", json={"refresh_token": login["refresh_token"]})).status_code == 401, "the old refresh token is dead")
        check((await c.post("/auth/refresh", json={"refresh_token": r.json()["refresh_token"]})).status_code == 401, "...and reusing it revoked the whole sign-in")
        fresh = (await c.post("/auth/login", json={"email": f"teacher@{dom}", "password": PW})).json()
        check((await c.post("/auth/logout", json={"refresh_token": fresh["refresh_token"]})).status_code == 204, "logout succeeds")
        check((await c.post("/auth/refresh", json={"refresh_token": fresh["refresh_token"]})).status_code == 401, "a logged-out refresh token is dead")
        live = (await c.post("/auth/login", json={"email": f"teacher@{dom}", "password": PW})).json()
        check((await c.patch(f"/admin/lecturers/{lec_id}", json={"is_active": False}, headers=bearer(admin_tok))).json()["is_active"] is False, "the admin deactivates the lecturer")
        check((await c.get("/rooms", headers=bearer(live["access_token"]))).status_code == 401, "their still-unexpired access token stops working immediately")
        check((await c.post("/auth/refresh", json={"refresh_token": live["refresh_token"]})).status_code == 401, "their refresh token stops working")
        check((await c.post("/auth/login", json={"email": f"teacher@{dom}", "password": PW})).status_code == 401, "and they can't sign in")

        print("\ntest_password_reset_over_http")
        email = f"two@{dom}"
        check((await c.post("/auth/password-reset/request", json={"email": email})).status_code == 202, "a reset request is accepted")
        check((await c.post("/auth/password-reset/request", json={"email": "ghost@" + dom})).status_code == 202 and not mailer.to("ghost@" + dom), "...with the same answer for an address that has no account, and no email")
        r = await c.post("/auth/password-reset/confirm", json={"email": email, "code": mailer.code(email), "password": "a brand new password"})
        check(r.status_code == 200, "the code resets the password")
        check((await c.post("/auth/login", json={"email": email, "password": PW})).status_code == 401, "the old password no longer works")
        check((await c.post("/auth/login", json={"email": email, "password": "a brand new password"})).status_code == 200, "the new one does")

    print("\ntest_rate_limiting")
    tight = create_app(cfg, db, acc, Limiters(login_email=RateLimiter(3, 900), code_attempts=RateLimiter(3, 900), register_ip=RateLimiter(2, 3600)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=tight), base_url="http://test") as t:
        codes = [(await t.post("/auth/login", json={"email": f"admin@{dom}", "password": "wrong password!!"})).status_code for _ in range(5)]
        check(codes == [401, 401, 401, 429, 429], f"repeated sign-in failures for one address are throttled ({codes})")
        codes = [(await t.post("/auth/activate", json={"email": f"admin@{dom}", "code": "000000", "password": PW})).status_code for _ in range(5)]
        check(codes[-1] == 429, "code guessing is throttled too")
        codes = [(await t.post("/auth/register", json={"email": f"r{i}@student.{dom}", "full_name": "R"})).status_code for i in range(4)]
        check(codes == [202, 202, 429, 429], f"sign-ups from one address are throttled ({codes})")


async def main() -> int:
    if not DSN:
        print("SKIPPED: set TEST_DATABASE_URL to run the API tests against a real PostgreSQL.")
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
