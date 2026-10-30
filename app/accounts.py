"""Accounts: who may exist, how they prove who they are, and how they stay signed in.

Hierarchy: one ADMIN per school, created by the platform operator. The admin invites LECTURERs. STUDENTs
register themselves. Nobody picks a password until they have proved they own their university mailbox: every
account starts without one and gets it from a one-time emailed code (the same step for a lecturer's invite and a
student's sign-up). That ordering also means nobody can register a victim's address with their own password and
wait for the victim to verify it.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

import asyncpg

from . import auth
from .db import AuthUser, Database, Room, new_join_code
from .email_sender import Mailer, activation_email, existing_account_email, reset_email

logger = logging.getLogger("accounts")

MAX_CODE_ATTEMPTS = 5
RESEND_COOLDOWN_S = 60


class AccountError(Exception):
    """Base class; the message is safe to show to the person who caused it."""


class EmailDomainNotAllowed(AccountError):
    pass


class EmailTaken(AccountError):
    pass


class Conflict(AccountError):
    pass


class NotFound(AccountError):
    pass


class TooSoon(AccountError):
    pass


class Accounts:
    def __init__(self, db: Database, secret: str, mailer: Mailer, *, code_ttl_s: int = 900,
                 refresh_ttl_s: int = 7 * 24 * 3600) -> None:
        self._db, self._pool, self._secret, self._mailer = db, db.pool, secret, mailer
        self._code_ttl, self._refresh_ttl = code_ttl_s, refresh_ttl_s

    # ---- schools and domains (platform operator) ---------------------------------

    async def create_institution(self, name: str, domain: str, admin_email: str, admin_name: str) -> tuple[uuid.UUID, uuid.UUID]:
        """Creates a school, registers its primary email domain, and invites its single admin."""
        admin_email = auth.normalize_email(admin_email)
        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    inst = await conn.fetchval(
                        "INSERT INTO institutions (name, domain, admin_email) VALUES ($1, lower($2), $3) RETURNING id",
                        name, domain, admin_email)
                    admin = await conn.fetchval(
                        "INSERT INTO users (institution_id, email, password_hash, full_name, role) "
                        "VALUES ($1, $2, NULL, $3, 'ADMIN') RETURNING id", inst, admin_email, admin_name)
        except asyncpg.UniqueViolationError as e:
            raise Conflict("That domain or admin email is already registered") from e
        except asyncpg.CheckViolationError as e:
            raise EmailDomainNotAllowed("The admin's email must be on the school's own domain") from e
        await self._send_code(admin, admin_email, admin_name, "ACTIVATE")
        return inst, admin

    async def add_domain(self, institution_id: uuid.UUID, domain: str, *, allow_staff: bool, allow_students: bool) -> None:
        try:
            await self._pool.execute(
                "INSERT INTO institution_email_domains (domain, institution_id, allow_staff, allow_students) VALUES (lower($1), $2, $3, $4)",
                domain, institution_id, allow_staff, allow_students)
        except asyncpg.UniqueViolationError as e:
            raise Conflict("That domain already belongs to a school") from e

    async def list_domains(self, institution_id: uuid.UUID) -> list[asyncpg.Record]:
        return await self._pool.fetch(
            "SELECT domain, allow_staff, allow_students FROM institution_email_domains WHERE institution_id = $1 ORDER BY domain",
            institution_id)

    async def _domain_owner(self, domain: str) -> Optional[asyncpg.Record]:
        return await self._pool.fetchrow(
            "SELECT institution_id, allow_staff, allow_students FROM institution_email_domains WHERE domain = $1", domain)

    # ---- creating accounts ---------------------------------------------------------

    async def _find_user(self, email: str) -> Optional[asyncpg.Record]:
        return await self._pool.fetchrow(
            "SELECT id, institution_id, email, full_name, role, is_active, password_hash, email_verified_at "
            "FROM users WHERE lower(email) = $1", email)

    async def _create_pending(self, institution_id: uuid.UUID, email: str, full_name: str, role: str) -> uuid.UUID:
        try:
            return await self._pool.fetchval(
                "INSERT INTO users (institution_id, email, password_hash, full_name, role) VALUES ($1, $2, NULL, $3, $4) RETURNING id",
                institution_id, email, full_name, role)
        except asyncpg.CheckViolationError as e:
            raise EmailDomainNotAllowed("That email domain isn't registered for this kind of account") from e
        except asyncpg.UniqueViolationError as e:
            if e.constraint_name == "one_admin_per_institution":
                raise Conflict("This school already has an admin account") from e
            raise EmailTaken("An account with this email already exists") from e

    async def register_student(self, email: str, full_name: str) -> None:
        """Starts a student sign-up. Same visible outcome whether or not the address already has an account
        (those cases get an email instead), so the endpoint can't be used to find out who is registered."""
        owner = await self._domain_owner(auth.email_domain(email))
        if owner is None or not owner["allow_students"]:
            raise EmailDomainNotAllowed("That email domain isn't registered with a school on BLB. Use your official university email.")
        existing = await self._find_user(email)
        if existing is None:
            user_id = await self._create_pending(owner["institution_id"], email, full_name, "STUDENT")
            await self._send_code(user_id, email, full_name, "ACTIVATE")
        elif existing["role"] == "STUDENT" and existing["is_active"] and existing["password_hash"] is None:
            await self._send_code(existing["id"], email, existing["full_name"], "ACTIVATE")  # still pending: resend
        else:
            subject, body = existing_account_email(existing["full_name"])
            await self._mailer.send(email, subject, body)

    async def invite_lecturer(self, institution_id: uuid.UUID, email: str, full_name: str) -> asyncpg.Record:
        owner = await self._domain_owner(auth.email_domain(email))
        if owner is None or owner["institution_id"] != institution_id or not owner["allow_staff"]:
            domains = [r["domain"] for r in await self.list_domains(institution_id) if r["allow_staff"]]
            raise EmailDomainNotAllowed("Lecturers must use one of your school's staff email domains: " + ", ".join(domains))
        user_id = await self._create_pending(institution_id, email, full_name, "LECTURER")
        await self._send_code(user_id, email, full_name, "ACTIVATE")
        return await self.get_lecturer(institution_id, user_id)

    async def resend_admin_invite(self, email: str) -> None:
        """Operator tool: a school's admin lost their invite before activating."""
        row = await self._find_user(email)
        if row is None or row["role"] != "ADMIN":
            raise NotFound("No admin account with that email")
        if row["password_hash"] is not None:
            raise Conflict("That admin has already activated; they can use 'forgot password'")
        if not await self._send_code(row["id"], email, row["full_name"], "ACTIVATE"):
            raise TooSoon("An invite was just sent. Wait a minute before sending another.")

    async def resend_invite(self, institution_id: uuid.UUID, user_id: uuid.UUID) -> None:
        row = await self.get_lecturer(institution_id, user_id)
        if row is None:
            raise NotFound("Lecturer not found")
        if row["email_verified_at"] is not None:
            raise Conflict("This lecturer has already activated their account")
        if not row["is_active"]:
            raise Conflict("This lecturer is deactivated")
        if not await self._send_code(user_id, row["email"], row["full_name"], "ACTIVATE"):
            raise TooSoon("An invite was just sent. Wait a minute before sending another.")

    # ---- one-time codes --------------------------------------------------------------

    async def _issue_code(self, user_id: uuid.UUID, purpose: str) -> Optional[str]:
        code = auth.new_code()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                recent = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM email_codes WHERE user_id = $1 AND purpose = $2 "
                    "               AND created_at > now() - make_interval(secs => $3))", user_id, purpose, float(RESEND_COOLDOWN_S))
                if recent:
                    return None
                await conn.execute("UPDATE email_codes SET consumed_at = now() WHERE user_id = $1 AND purpose = $2 AND consumed_at IS NULL",
                                   user_id, purpose)
                await conn.execute(
                    "INSERT INTO email_codes (user_id, purpose, code_hash, expires_at) VALUES ($1, $2, $3, now() + make_interval(secs => $4))",
                    user_id, purpose, auth.hash_code(code, user_id, purpose, self._secret), float(self._code_ttl))
        return code

    async def _send_code(self, user_id: uuid.UUID, email: str, name: str, purpose: str) -> bool:
        """False when the resend cooldown suppressed it."""
        code = await self._issue_code(user_id, purpose)
        if code is None:
            return False
        make = reset_email if purpose == "RESET_PASSWORD" else activation_email
        subject, body = make(name, code, self._code_ttl // 60)
        await self._mailer.send(email, subject, body)
        return True

    async def _consume_code(self, conn, user_id: uuid.UUID, purpose: str, code: str) -> bool:
        row = await conn.fetchrow(
            "SELECT id, code_hash, attempts FROM email_codes WHERE user_id = $1 AND purpose = $2 AND consumed_at IS NULL "
            "AND expires_at > now() ORDER BY created_at DESC LIMIT 1 FOR UPDATE", user_id, purpose)
        if row is None or row["attempts"] >= MAX_CODE_ATTEMPTS:
            return False
        if auth.codes_match(row["code_hash"], auth.hash_code(code, user_id, purpose, self._secret)):
            await conn.execute("UPDATE email_codes SET consumed_at = now() WHERE id = $1", row["id"])
            return True
        await conn.execute("UPDATE email_codes SET attempts = attempts + 1 WHERE id = $1", row["id"])
        return False

    # ---- activation, sign-in, reset ---------------------------------------------------

    async def activate_account(self, email: str, code: str, password: str) -> Optional[AuthUser]:
        """Proves mailbox ownership and sets the first password in one transaction. None for any failure."""
        row = await self._find_user(email)
        if row is None or not row["is_active"] or row["password_hash"] is not None:
            return None
        pw_hash = await asyncio.to_thread(auth.hash_password, password)
        ok = False
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                ok = await self._consume_code(conn, row["id"], "ACTIVATE", code)
                if ok:
                    await conn.execute("UPDATE users SET password_hash = $2, email_verified_at = now() "
                                       "WHERE id = $1 AND password_hash IS NULL", row["id"], pw_hash)
        return await self._db.get_active_user(row["id"]) if ok else None

    async def authenticate(self, email: str, password: str) -> Optional[AuthUser]:
        row = await self._find_user(email)
        ok = await asyncio.to_thread(auth.verify_password, row["password_hash"] if row else None, password)
        if not ok or row is None:
            return None
        return await self._db.get_active_user(row["id"])

    async def request_password_reset(self, email: str) -> None:
        row = await self._find_user(email)
        if row is not None and row["is_active"] and row["password_hash"] is not None:
            await self._send_code(row["id"], email, row["full_name"], "RESET_PASSWORD")

    async def reset_password(self, email: str, code: str, password: str) -> bool:
        row = await self._find_user(email)
        if row is None or not row["is_active"] or row["password_hash"] is None:
            return False
        pw_hash = await asyncio.to_thread(auth.hash_password, password)
        ok = False
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                ok = await self._consume_code(conn, row["id"], "RESET_PASSWORD", code)
                if ok:
                    await conn.execute("UPDATE users SET password_hash = $2 WHERE id = $1", row["id"], pw_hash)
                    await conn.execute("UPDATE refresh_tokens SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL", row["id"])
        return ok

    # ---- refresh tokens ----------------------------------------------------------------

    async def start_session(self, user_id: uuid.UUID) -> str:
        token = auth.new_refresh_token()
        await self._pool.execute(
            "INSERT INTO refresh_tokens (user_id, family_id, token_hash, expires_at) VALUES ($1, $2, $3, now() + make_interval(secs => $4))",
            user_id, uuid.uuid4(), auth.hash_token(token), float(self._refresh_ttl))
        return token

    async def rotate_refresh(self, token: str) -> Optional[tuple[AuthUser, str]]:
        """Exchanges a refresh token for a new one. Presenting a token that was already exchanged means it
        leaked (or was replayed), so every token from that sign-in is revoked."""
        new_token = auth.new_refresh_token()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id, user_id, family_id, revoked_at, expires_at <= now() AS expired FROM refresh_tokens "
                    "WHERE token_hash = $1 FOR UPDATE", auth.hash_token(token))
                if row is None:
                    return None
                if row["revoked_at"] is not None:
                    await conn.execute("UPDATE refresh_tokens SET revoked_at = now() WHERE family_id = $1 AND revoked_at IS NULL", row["family_id"])
                    logger.warning("Refresh token reuse detected for user %s; revoked the whole sign-in", row["user_id"])
                    return None
                if row["expired"]:
                    return None
                await conn.execute("UPDATE refresh_tokens SET revoked_at = now() WHERE id = $1", row["id"])
                await conn.execute(
                    "INSERT INTO refresh_tokens (user_id, family_id, token_hash, expires_at) VALUES ($1, $2, $3, now() + make_interval(secs => $4))",
                    row["user_id"], row["family_id"], auth.hash_token(new_token), float(self._refresh_ttl))
                user_id, family_id = row["user_id"], row["family_id"]
        user = await self._db.get_active_user(user_id)
        if user is None:  # deactivated, or the domain was removed, since they signed in
            await self._pool.execute("UPDATE refresh_tokens SET revoked_at = now() WHERE family_id = $1 AND revoked_at IS NULL", family_id)
            return None
        return user, new_token

    async def revoke_refresh(self, token: str) -> None:
        await self._pool.execute(
            "UPDATE refresh_tokens SET revoked_at = now() WHERE revoked_at IS NULL AND family_id = "
            "(SELECT family_id FROM refresh_tokens WHERE token_hash = $1)", auth.hash_token(token))

    # ---- admin: lecturers ------------------------------------------------------------------

    async def list_lecturers(self, institution_id: uuid.UUID) -> list[asyncpg.Record]:
        return await self._pool.fetch(
            "SELECT id, email, full_name, role, is_active, email_verified_at, created_at FROM users "
            "WHERE institution_id = $1 AND role = 'LECTURER' ORDER BY lower(full_name)", institution_id)

    async def get_lecturer(self, institution_id: uuid.UUID, user_id: uuid.UUID) -> Optional[asyncpg.Record]:
        return await self._pool.fetchrow(
            "SELECT id, email, full_name, role, is_active, email_verified_at, created_at FROM users "
            "WHERE id = $1 AND institution_id = $2 AND role = 'LECTURER'", user_id, institution_id)

    async def update_lecturer(self, institution_id: uuid.UUID, user_id: uuid.UUID, *, is_active: Optional[bool] = None,
                              full_name: Optional[str] = None) -> Optional[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "UPDATE users SET is_active = COALESCE($3, is_active), full_name = COALESCE($4, full_name) "
                    "WHERE id = $1 AND institution_id = $2 AND role = 'LECTURER' "
                    "RETURNING id, email, full_name, role, is_active, email_verified_at, created_at", user_id, institution_id, is_active, full_name)
                if row is not None and is_active is False:
                    await conn.execute("UPDATE refresh_tokens SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL", user_id)
        return row

    # ---- rooms -------------------------------------------------------------------------------

    async def list_rooms(self, institution_id: uuid.UUID) -> list[asyncpg.Record]:
        return await self._pool.fetch(
            "SELECT r.id, r.room_code, r.room_name, r.join_code, r.assigned_lecturer_id, u.full_name AS lecturer_name, "
            "       EXISTS (SELECT 1 FROM lecture_sessions s WHERE s.room_id = r.id AND s.status = 'LIVE') AS is_live "
            "FROM rooms r LEFT JOIN users u ON u.id = r.assigned_lecturer_id "
            "WHERE r.institution_id = $1 ORDER BY lower(r.room_code)", institution_id)

    async def get_room(self, institution_id: uuid.UUID, room_id: uuid.UUID) -> Optional[asyncpg.Record]:
        return await self._pool.fetchrow(
            "SELECT id, institution_id, room_code, room_name, join_code, assigned_lecturer_id FROM rooms WHERE id = $1 AND institution_id = $2",
            room_id, institution_id)

    async def create_room(self, institution_id: uuid.UUID, creator_id: uuid.UUID, room_code: str, room_name: Optional[str]) -> Room:
        try:
            return await self._db.create_room(institution_id, room_code.strip(), room_name, creator_id)
        except asyncpg.UniqueViolationError as e:
            raise Conflict("A room with that label already exists") from e

    async def update_room(self, institution_id: uuid.UUID, room_id: uuid.UUID, room_name: Optional[str]) -> Optional[asyncpg.Record]:
        return await self._pool.fetchrow(
            "UPDATE rooms SET room_name = $3 WHERE id = $1 AND institution_id = $2 "
            "RETURNING id, institution_id, room_code, room_name, join_code, assigned_lecturer_id", room_id, institution_id, room_name)

    async def rotate_join_code(self, institution_id: uuid.UUID, room_id: uuid.UUID) -> Optional[str]:
        for _ in range(8):
            try:
                return await self._pool.fetchval(
                    "UPDATE rooms SET join_code = $3 WHERE id = $1 AND institution_id = $2 RETURNING join_code",
                    room_id, institution_id, new_join_code())
            except asyncpg.UniqueViolationError:
                continue
        raise RuntimeError("could not generate a unique join code")

    # ---- admin dashboard ---------------------------------------------------------------------------

    async def recent_sessions(self, institution_id: uuid.UUID, limit: int) -> list[asyncpg.Record]:
        return await self._pool.fetch(
            "SELECT s.id, s.status, s.started_at, s.ended_at, s.duration_seconds, s.end_reason, r.room_code, u.full_name AS lecturer_name "
            "FROM lecture_sessions s JOIN rooms r ON r.id = s.room_id JOIN users u ON u.id = s.lecturer_id "
            "WHERE s.institution_id = $1 ORDER BY s.started_at DESC LIMIT $2", institution_id, limit)
