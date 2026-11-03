"""Credentials and tokens.

Passwords: argon2id. Access tokens: short-lived HS256 JWTs that carry the role. Refresh tokens: opaque random
strings, stored only as SHA-256 hashes (rotation lives in accounts.py). One-time email codes: 6 digits, stored
only as an HMAC keyed with the server secret.

The role inside an access token is a convenience for the client. Whoever checks a token (the HTTP API and the
WebSocket handshake) re-reads the account from the database, so a deactivated account, a changed role or a
removed email domain takes effect at once rather than when the token expires.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


class AuthError(Exception):
    pass


ACCESS = "access"
ROLES = ("ADMIN", "LECTURER", "STUDENT")
TEACHING_ROLES = ("ADMIN", "LECTURER")  # who may start a lecture (admins can teach too)
MIN_PASSWORD, MAX_PASSWORD = 10, 128


@dataclass(frozen=True)
class TokenClaims:
    user_id: uuid.UUID
    role: str
    institution_id: uuid.UUID


def issue_access_token(user_id, role: str, institution_id, secret: str, ttl_s: int = 900) -> str:
    now = int(time.time())
    return jwt.encode({"sub": str(user_id), "role": role, "inst": str(institution_id), "typ": ACCESS,
                       "iat": now, "exp": now + ttl_s}, secret, algorithm="HS256")


def verify_access_token(token: str, secret: str) -> TokenClaims:
    if not token:
        raise AuthError("missing token")
    try:
        c = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "sub", "typ", "role", "inst"]})
    except jwt.PyJWTError as e:
        raise AuthError(str(e)) from e
    if c.get("typ") != ACCESS or c.get("role") not in ROLES:
        raise AuthError("not an access token")
    try:
        return TokenClaims(uuid.UUID(str(c["sub"])), c["role"], uuid.UUID(str(c["inst"])))
    except ValueError as e:
        raise AuthError("malformed token") from e


# ---- passwords ---------------------------------------------------------------
_hasher = PasswordHasher()  # argon2id with the library's current defaults
_DUMMY_HASH = _hasher.hash("not-a-real-password")


def check_password_policy(password: str, email: Optional[str] = None) -> Optional[str]:
    """Returns a message for the user, or None if the password is acceptable."""
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if len(password) > MAX_PASSWORD:
        return f"Use at most {MAX_PASSWORD} characters."
    if len(set(password)) < 4:
        return "That password is too repetitive."
    if email and (password.lower() == email.lower() or password.lower() == email.split("@")[0].lower()):
        return "Don't use your email address as your password."
    return None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: Optional[str], password: str) -> bool:
    """Always does one argon2 verification, even for an account that doesn't exist or has no password yet,
    so response time doesn't reveal which emails are registered."""
    try:
        matched = _hasher.verify(stored or _DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return bool(matched) and stored is not None


# ---- opaque tokens and one-time codes ------------------------------------------
def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(code: str, user_id, purpose: str, secret: str) -> str:
    return hmac.new(secret.encode(), f"{user_id}:{purpose}:{code}".encode(), hashlib.sha256).hexdigest()


def codes_match(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


# ---- email addresses -----------------------------------------------------------
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")


def normalize_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if len(email) > 254 or not _EMAIL_RE.match(email):
        raise ValueError("Enter a valid email address.")
    return email


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[1]
