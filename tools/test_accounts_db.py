#!/usr/bin/env python3
"""
The account rules against a REAL PostgreSQL: the email-domain trigger, one admin per school, one-time codes,
activation, sign-in, refresh-token rotation, password reset, lecturer invites and tenant isolation.
Needs TEST_DATABASE_URL (a throwaway schema is created and dropped); skipped otherwise.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import asyncpg  # noqa: E402

from app.accounts import (Accounts, Conflict, EmailDomainNotAllowed, EmailTaken, TooSoon)  # noqa: E402
from app.db import Database  # noqa: E402
from test_db_layer import Check, FK, Seed, Unique, raises  # noqa: E402

DSN = os.getenv("TEST_DATABASE_URL")
SECRET = "test-secret-test-secret-test-secret-0123456789"
PW = "correct horse battery"
FAILURES = 0


def check(cond: bool, desc: str) -> None:
    global FAILURES
    print(f"  [{'PASS' if cond else 'FAIL'}] {desc}")
    if not cond:
        FAILURES += 1


class CapturingMailer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, to, subject, body):
        self.sent.append((to, subject, body))

    def to(self, addr):
        return [m for m in self.sent if m[0] == addr]

    def code(self, addr) -> str:
        return re.search(r"\b(\d{6})\b", self.to(addr)[-1][2]).group(1)


def wrong_code(real: str) -> str:
    return "000000" if real != "000000" else "111111"


async def raw_user(pool, inst, email, role="LECTURER", pw=None, verified=False):
    return await pool.fetchval(
        "INSERT INTO users (institution_id, email, password_hash, full_name, role, email_verified_at) "
        "VALUES ($1, $2, $3, 'T', $4, CASE WHEN $5::boolean THEN now() END) RETURNING id", inst, email, pw, role, verified)


async def test_database_rules(s: Seed) -> None:
    print("\ntest_database_enforces_the_email_rules")
    pool = s.pool
    a, b = await s.institution(), await s.institution()
    da = await pool.fetchval("SELECT domain FROM institutions WHERE id = $1", a)
    db_ = await pool.fetchval("SELECT domain FROM institutions WHERE id = $1", b)
    check(await pool.fetchval("SELECT count(*) FROM institution_email_domains WHERE institution_id = $1 AND domain = $2", a, da) == 1,
          "a new school's primary domain is registered automatically")
    await pool.execute("INSERT INTO institution_email_domains (domain, institution_id, allow_staff, allow_students) VALUES ($1,$2,true,false)", f"staff.{da}", a)
    await pool.execute("INSERT INTO institution_email_domains (domain, institution_id, allow_staff, allow_students) VALUES ($1,$2,false,true)", f"student.{da}", a)
    check(await raises(Check, raw_user(pool, a, f"x@gmail.com", "STUDENT")), "a student on an unregistered domain is rejected by the database")
    check(await raises(Check, raw_user(pool, a, f"x@{db_}", "STUDENT")), "an email on ANOTHER school's domain is rejected")
    check(await raises(Check, raw_user(pool, a, f"x@staff.{da}", "STUDENT")), "a student can't use a staff-only domain")
    check(await raises(Check, raw_user(pool, a, f"x@student.{da}", "LECTURER")), "a lecturer can't use a student-only domain")
    await raw_user(pool, a, f"ok@student.{da}", "STUDENT")
    await raw_user(pool, a, f"ok@staff.{da}", "LECTURER")
    check(True, "students on student domains and staff on staff domains are accepted")
    check(await raises(Check, pool.execute("UPDATE users SET email = 'moved@gmail.com' WHERE lower(email) = $1", f"ok@staff.{da}")),
          "changing an existing email to a bad domain is rejected too")
    check(await raises(Unique, pool.execute("INSERT INTO institution_email_domains (domain, institution_id) VALUES ($1,$2)", da, b)),
          "a domain can belong to only one school")
    await raw_user(pool, a, f"admin@{da}", "ADMIN")
    check(await raises(Unique, raw_user(pool, a, f"admin2@{da}", "ADMIN")), "a school can't have a second admin")
    await raw_user(pool, b, f"admin@{db_}", "ADMIN")
    check(True, "but every school has its own")
    check(await raises(Unique, raw_user(pool, a, f"ADMIN@{da.upper()}", "LECTURER")), "email addresses are unique regardless of case")
    check(await raises(Check, raw_user(pool, a, f"v@{da}", verified=True, pw=None)), "an account can't be verified without a password")

    print("\ntest_get_active_user_is_strict")
    db = s.db
    ok = await raw_user(pool, a, f"live@{da}", pw="x", verified=True)
    pending = await raw_user(pool, a, f"pend@{da}")
    off = await raw_user(pool, a, f"off@{da}", pw="x", verified=True)
    await pool.execute("UPDATE users SET is_active = false WHERE id = $1", off)
    check((await db.get_active_user(ok)) is not None, "a verified, active account on a registered domain is accepted")
    check(await db.get_active_user(pending) is None, "an unverified account is not")
    check(await db.get_active_user(off) is None, "a deactivated account is not")
    gone_inst = await s.institution()
    gd = await pool.fetchval("SELECT domain FROM institutions WHERE id = $1", gone_inst)
    gone = await raw_user(pool, gone_inst, f"g@{gd}", pw="x", verified=True)
    check(await db.get_active_user(gone) is not None, "(control) accepted while its domain is registered")
    await pool.execute("DELETE FROM institution_email_domains WHERE domain = $1", gd)
    check(await db.get_active_user(gone) is None, "removing the school's domain locks its accounts out immediately")


async def test_signup_and_activation(acc: Accounts, mailer: CapturingMailer, pool) -> dict:
    print("\ntest_registration_and_activation")
    dom = f"u{uuid.uuid4().hex[:8]}.example"
    inst, admin_id = await acc.create_institution("Test University", dom, f"admin@{dom}", "Ada Admin")
    row = await pool.fetchrow("SELECT password_hash, email_verified_at, role FROM users WHERE id = $1", admin_id)
    check(row["password_hash"] is None and row["email_verified_at"] is None and row["role"] == "ADMIN", "the school's admin starts invited, with no password")
    check(len(mailer.to(f"admin@{dom}")) == 1, "the admin's invite was emailed")
    check(await raises(Conflict, acc.create_institution("Again", dom, f"admin2@{dom}", "X")), "a domain can't be registered twice")
    check(await raises(EmailDomainNotAllowed, acc.create_institution("Odd", f"o{dom}", "admin@gmail.com", "X")), "an admin must use the school's own domain")
    admin = await acc.activate_account(f"admin@{dom}", mailer.code(f"admin@{dom}"), PW)
    check(admin is not None and admin.role == "ADMIN", "the admin activates with the emailed code and chooses a password")

    await acc.add_domain(inst, f"student.{dom}", allow_staff=False, allow_students=True)
    await acc.add_domain(inst, f"staff.{dom}", allow_staff=True, allow_students=False)
    check(await raises(EmailDomainNotAllowed, acc.register_student(f"kim@gmail.com", "Kim")), "registering with a non-university email is refused")
    check(await raises(EmailDomainNotAllowed, acc.register_student(f"kim@staff.{dom}", "Kim")), "registering on a staff-only domain is refused")

    kim = f"kim@student.{dom}"
    await acc.register_student(kim, "Kim Student")
    pending = await pool.fetchrow("SELECT password_hash, email_verified_at, role FROM users WHERE lower(email) = $1", kim)
    check(pending["role"] == "STUDENT" and pending["password_hash"] is None, "sign-up creates a student with NO password yet (so it can't be hijacked)")
    check(len(mailer.to(kim)) == 1, "a code was emailed")
    await acc.register_student(kim, "Someone Else")
    check(len(mailer.to(kim)) == 1, "registering again straight away doesn't send another (resend cooldown)")
    real = mailer.code(kim)
    for _ in range(5):
        await acc.activate_account(kim, wrong_code(real), PW)
    check(await acc.activate_account(kim, real, PW) is None, "after 5 wrong guesses even the right code is refused")
    await pool.execute("UPDATE email_codes SET created_at = now() - interval '2 minutes'")
    await acc.register_student(kim, "Kim Student")
    check(len(mailer.to(kim)) == 2, "after the cooldown a fresh code is sent")
    check(await acc.activate_account(kim, real, PW) is None, "the old code no longer works")
    user = await acc.activate_account(kim, mailer.code(kim), PW)
    check(user is not None and user.role == "STUDENT" and user.institution_id == inst, "the new code activates the account in the right school")
    check(await acc.activate_account(kim, mailer.code(kim), "another password!!") is None, "a used code can't be replayed")
    check((await acc.authenticate(kim, PW)) is not None, "the student can sign in with the password they chose")
    check(await acc.authenticate(kim, "wrong password!!") is None, "a wrong password is refused")
    check(await acc.authenticate(f"nobody@student.{dom}", PW) is None, "an unknown email is refused the same way")
    before = len(mailer.to(kim))
    await acc.register_student(kim, "Kim Student")
    check(len(mailer.to(kim)) == before + 1 and "already" in mailer.to(kim)[-1][1], "registering an existing address sends a notice, not a code")

    lee = f"lee@student.{dom}"
    await acc.register_student(lee, "Lee")
    await pool.execute("UPDATE email_codes SET expires_at = now() - interval '1 minute' WHERE user_id = (SELECT id FROM users WHERE lower(email) = $1)", lee)
    check(await acc.activate_account(lee, mailer.code(lee), PW) is None, "an expired code is refused")
    return {"inst": inst, "dom": dom, "admin": admin, "kim": user, "kim_email": kim}


async def test_sessions_and_reset(acc: Accounts, mailer: CapturingMailer, pool, ctx: dict) -> None:
    print("\ntest_refresh_tokens_and_password_reset")
    kim, admin, dom = ctx["kim"], ctx["admin"], ctx["dom"]
    t1 = await acc.start_session(kim.id)
    r = await acc.rotate_refresh(t1)
    check(r is not None and r[0].id == kim.id and r[1] != t1, "a refresh token is exchanged for a new one")
    t2 = r[1]
    check(await acc.rotate_refresh(t1) is None, "reusing the old token is refused")
    check(await acc.rotate_refresh(t2) is None, "...and it revokes the whole sign-in, including the newer token")
    check(await acc.rotate_refresh("not-a-token") is None, "an unknown token is refused")
    t3 = await acc.start_session(kim.id)
    await pool.execute("UPDATE refresh_tokens SET expires_at = now() - interval '1 second' WHERE token_hash IS NOT NULL AND user_id = $1 AND revoked_at IS NULL", kim.id)
    check(await acc.rotate_refresh(t3) is None, "an expired token is refused")
    t4 = await acc.start_session(kim.id)
    await acc.revoke_refresh(t4)
    check(await acc.rotate_refresh(t4) is None, "logging out revokes the token")

    email = f"admin@{dom}"
    await acc.request_password_reset("nobody@" + dom)
    check(len(mailer.to("nobody@" + dom)) == 0, "a reset for an unknown address sends nothing")
    old_session = await acc.start_session(admin.id)
    await acc.request_password_reset(email)
    check("Reset" in mailer.to(email)[-1][1], "a reset code is emailed to a real account")
    code = mailer.code(email)
    check(await acc.reset_password(email, wrong_code(code), "brand new password") is False, "a wrong reset code is refused")
    check(await acc.reset_password(email, code, "brand new password") is True, "the right code sets the new password")
    check(await acc.authenticate(email, PW) is None and await acc.authenticate(email, "brand new password") is not None, "the old password stops working")
    check(await acc.rotate_refresh(old_session) is None, "a password reset signs out every existing session")
    check(await acc.reset_password(email, code, "yet another password") is False, "a reset code is single-use")


async def test_lecturer_management(acc: Accounts, mailer: CapturingMailer, pool, ctx: dict) -> None:
    print("\ntest_lecturer_invites_and_tenant_isolation")
    inst, dom = ctx["inst"], ctx["dom"]
    odom = f"o{uuid.uuid4().hex[:8]}.example"
    oinst, oadmin = await acc.create_institution("Other U", odom, f"admin@{odom}", "Other Admin")
    check(await raises(EmailDomainNotAllowed, acc.invite_lecturer(inst, f"t@student.{dom}", "T")), "a lecturer can't be invited on a student-only domain")
    check(await raises(EmailDomainNotAllowed, acc.invite_lecturer(inst, f"t@{odom}", "T")), "a lecturer can't be invited from another school's domain")
    lec = await acc.invite_lecturer(inst, f"teacher@{dom}", "Tara Teacher")
    check(lec["role"] == "LECTURER" and lec["email_verified_at"] is None and len(mailer.to(f"teacher@{dom}")) == 1, "the admin invites a lecturer; the invite is emailed")
    check(await raises(EmailTaken, acc.invite_lecturer(inst, f"teacher@{dom}", "Again")), "inviting the same address twice is refused")
    check(await raises(TooSoon, acc.resend_invite(inst, lec["id"])), "resending immediately is throttled")
    await pool.execute("UPDATE email_codes SET created_at = now() - interval '2 minutes'")
    await acc.resend_invite(inst, lec["id"])
    check(len(mailer.to(f"teacher@{dom}")) == 2, "after the cooldown the invite can be resent")
    teacher = await acc.activate_account(f"teacher@{dom}", mailer.code(f"teacher@{dom}"), PW)
    check(teacher is not None and teacher.role == "LECTURER", "the lecturer activates and chooses their own password (the admin never sees it)")
    check(await raises(Conflict, acc._create_pending(inst, f"a2@{dom}", "Second", "ADMIN")), "a second admin can't be created")

    check(await acc.get_lecturer(oinst, lec["id"]) is None, "another school's admin can't see this lecturer")
    check(await acc.update_lecturer(oinst, lec["id"], is_active=False) is None, "...or deactivate them")
    check(len(await acc.list_lecturers(oinst)) == 0 and len(await acc.list_lecturers(inst)) == 1, "lecturer lists are per school")
    session = await acc.start_session(teacher.id)
    off = await acc.update_lecturer(inst, lec["id"], is_active=False)
    check(off["is_active"] is False, "the admin deactivates a lecturer")
    check(await acc.authenticate(f"teacher@{dom}", PW) is None, "a deactivated lecturer can't sign in")
    check(await acc.rotate_refresh(session) is None, "and their existing sessions stop at once")

    room = await acc.create_room(inst, teacher.id, "LAB-1", "Lab one")
    check(room.join_code and len(await acc.list_rooms(inst)) == 1 and len(await acc.list_rooms(oinst)) == 0, "rooms are created per school")
    check(await raises(Conflict, acc.create_room(inst, teacher.id, "lab-1", None)), "a duplicate room label is refused")
    old = room.join_code
    new = await acc.rotate_join_code(inst, room.id)
    check(new != old and await acc.rotate_join_code(oinst, room.id) is None, "a join code can be rotated, but only by its own school")


async def main() -> int:
    if not DSN:
        print("SKIPPED: set TEST_DATABASE_URL to run the account tests against a real PostgreSQL.")
        return 0
    schema = "t_" + uuid.uuid4().hex[:10]
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    db = await Database.connect(DSN, search_path=schema)
    try:
        await db.migrate(ROOT / "migrations")
        mailer = CapturingMailer()
        acc = Accounts(db, SECRET, mailer, code_ttl_s=900, refresh_ttl_s=3600)
        await test_database_rules(Seed(db))
        ctx = await test_signup_and_activation(acc, mailer, db.pool)
        await test_sessions_and_reset(acc, mailer, db.pool, ctx)
        await test_lecturer_management(acc, mailer, db.pool, ctx)
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
