"""HTTP API: sign-up, sign-in, and the admin / lecturer / student operations.

Every route that needs a role goes through `require(...)`, which checks the bearer token AND re-reads the
account from the database (active, email verified, domain still registered, role unchanged). Every query is
scoped to the caller's school, so one school can never see or change another's data; asking for another
school's record answers 404, the same as a record that doesn't exist.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import auth
from .accounts import AccountError, Accounts, Conflict, EmailDomainNotAllowed, EmailTaken, NotFound, TooSoon
from .config import Config
from .db import AuthUser, Database
from .ratelimit import RateLimiter

logger = logging.getLogger("api")


@dataclass
class Limiters:
    login_email: RateLimiter = field(default_factory=lambda: RateLimiter(10, 900))
    login_ip: RateLimiter = field(default_factory=lambda: RateLimiter(40, 900))
    code_attempts: RateLimiter = field(default_factory=lambda: RateLimiter(10, 900))
    register_ip: RateLimiter = field(default_factory=lambda: RateLimiter(10, 3600))
    reset_email: RateLimiter = field(default_factory=lambda: RateLimiter(5, 3600))
    reset_ip: RateLimiter = field(default_factory=lambda: RateLimiter(20, 3600))


@dataclass
class Ctx:
    cfg: Config
    db: Database
    accounts: Accounts
    limits: Limiters


# ---- request models ---------------------------------------------------------------------------
def _clean_name(v: str) -> str:
    v = " ".join(v.split())
    if not v:
        raise ValueError("Enter a name.")
    return v


class EmailIn(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return auth.normalize_email(v)


class RegisterIn(EmailIn):
    full_name: str = Field(max_length=120)

    @field_validator("full_name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _clean_name(v)


class ActivateIn(EmailIn):
    code: str = Field(min_length=4, max_length=12)
    password: str = Field(max_length=200)


class LoginIn(EmailIn):
    password: str = Field(max_length=200)


class RefreshIn(BaseModel):
    refresh_token: str = Field(max_length=200)


class LecturerIn(EmailIn):
    full_name: str = Field(max_length=120)

    @field_validator("full_name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _clean_name(v)


class LecturerPatch(BaseModel):
    is_active: Optional[bool] = None
    full_name: Optional[str] = Field(None, max_length=120)

    @field_validator("full_name")
    @classmethod
    def _name(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _clean_name(v)


class RoomIn(BaseModel):
    room_code: str = Field(min_length=1, max_length=40)
    room_name: Optional[str] = Field(None, max_length=120)


class RoomPatch(BaseModel):
    room_name: Optional[str] = Field(None, max_length=120)


# ---- helpers --------------------------------------------------------------------------------------
def _http(e: AccountError) -> HTTPException:
    status = {EmailDomainNotAllowed: 422, EmailTaken: 409, Conflict: 409, NotFound: 404, TooSoon: 429}.get(type(e), 400)
    return HTTPException(status, str(e))


def _limit(limiter: RateLimiter, key) -> None:
    if not limiter.allow(key):
        raise HTTPException(429, "Too many attempts. Please wait a while and try again.", headers={"Retry-After": "900"})


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _user_out(u: AuthUser) -> dict:
    return {"id": str(u.id), "email": u.email, "full_name": u.full_name, "role": u.role, "institution_id": str(u.institution_id)}


def _rec(row) -> dict:
    return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in dict(row).items()}


def _tokens(ctx: Ctx, user: AuthUser, refresh_token: str) -> dict:
    cfg = ctx.cfg
    return {"access_token": auth.issue_access_token(user.id, user.role, user.institution_id, cfg.jwt_secret, cfg.access_token_ttl_s),
            "refresh_token": refresh_token, "token_type": "bearer", "expires_in": cfg.access_token_ttl_s, "user": _user_out(user)}


def _password_problem(password: str, email: str) -> None:
    msg = auth.check_password_policy(password, email)
    if msg:
        raise HTTPException(422, msg)


def create_app(cfg: Config, db: Database, accounts: Accounts, limits: Optional[Limiters] = None) -> FastAPI:
    ctx = Ctx(cfg, db, accounts, limits or Limiters())
    app = FastAPI(title="BLB API", version="1.0")
    app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in cfg.cors_origins.split(",") if o.strip()],
                       allow_methods=["*"], allow_headers=["Authorization", "Content-Type"], allow_credentials=False)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        msgs = [str(e.get("msg", "")).replace("Value error, ", "") for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": "; ".join(dict.fromkeys(msgs)) or "Invalid request"})

    async def current_user(authorization: Optional[str] = Header(None)) -> AuthUser:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Sign in required.", headers={"WWW-Authenticate": "Bearer"})
        try:
            claims = auth.verify_access_token(authorization[7:].strip(), cfg.jwt_secret)
        except auth.AuthError:
            raise HTTPException(401, "Your session has expired. Sign in again.", headers={"WWW-Authenticate": "Bearer"})
        user = await db.get_active_user(claims.user_id)
        if user is None or user.role != claims.role:
            raise HTTPException(401, "Your session is no longer valid. Sign in again.", headers={"WWW-Authenticate": "Bearer"})
        return user

    def require(*roles: str):
        async def dep(user: AuthUser = Depends(current_user)) -> AuthUser:
            if user.role not in roles:
                raise HTTPException(403, "Your account type can't do that.")
            return user
        return dep

    admin_only, staff, student_only = require("ADMIN"), require("ADMIN", "LECTURER"), require("STUDENT")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # ---- sign-up and sign-in ---------------------------------------------------------------------------
    @app.post("/auth/register", status_code=202)
    async def register(body: RegisterIn, request: Request):
        _limit(ctx.limits.register_ip, _ip(request))
        try:
            await accounts.register_student(body.email, body.full_name)
        except AccountError as e:
            raise _http(e)
        return {"message": "If this address can be registered, we've emailed it a verification code."}

    @app.post("/auth/activate")
    async def activate(body: ActivateIn, request: Request):
        _limit(ctx.limits.code_attempts, (body.email, _ip(request)))
        _password_problem(body.password, body.email)
        user = await accounts.activate_account(body.email, body.code.strip(), body.password)
        if user is None:
            raise HTTPException(400, "That code is wrong or has expired. Request a new one if needed.")
        return _tokens(ctx, user, await accounts.start_session(user.id))

    @app.post("/auth/login")
    async def login(body: LoginIn, request: Request):
        _limit(ctx.limits.login_ip, _ip(request))
        _limit(ctx.limits.login_email, body.email)
        user = await accounts.authenticate(body.email, body.password)
        if user is None:
            raise HTTPException(401, "Incorrect email or password. If you haven't chosen a password yet, use the code we emailed you.")
        ctx.limits.login_email.clear(body.email)
        return _tokens(ctx, user, await accounts.start_session(user.id))

    @app.post("/auth/refresh")
    async def refresh(body: RefreshIn):
        rotated = await accounts.rotate_refresh(body.refresh_token)
        if rotated is None:
            raise HTTPException(401, "Your session has expired. Sign in again.")
        user, new_token = rotated
        return _tokens(ctx, user, new_token)

    @app.post("/auth/logout", status_code=204)
    async def logout(body: RefreshIn):
        await accounts.revoke_refresh(body.refresh_token)
        return Response(status_code=204)

    @app.post("/auth/password-reset/request", status_code=202)
    async def reset_request(body: EmailIn, request: Request):
        _limit(ctx.limits.reset_ip, _ip(request))
        if ctx.limits.reset_email.allow(body.email):  # past the per-address limit we just stay quiet
            await accounts.request_password_reset(body.email)
        return {"message": "If that address has an account, we've emailed it a reset code."}

    @app.post("/auth/password-reset/confirm")
    async def reset_confirm(body: ActivateIn, request: Request):
        _limit(ctx.limits.code_attempts, (body.email, _ip(request)))
        _password_problem(body.password, body.email)
        if not await accounts.reset_password(body.email, body.code.strip(), body.password):
            raise HTTPException(400, "That code is wrong or has expired. Request a new one if needed.")
        return {"message": "Your password has been changed. Sign in with the new one."}

    @app.get("/me")
    async def me(user: AuthUser = Depends(current_user)):
        name = await db.pool.fetchval("SELECT name FROM institutions WHERE id = $1", user.institution_id)
        return {**_user_out(user), "institution_name": name}

    # ---- admin ---------------------------------------------------------------------------------------------
    @app.get("/admin/lecturers")
    async def list_lecturers(user: AuthUser = Depends(admin_only)):
        return [_rec(r) for r in await accounts.list_lecturers(user.institution_id)]

    @app.post("/admin/lecturers", status_code=201)
    async def invite_lecturer(body: LecturerIn, user: AuthUser = Depends(admin_only)):
        try:
            return _rec(await accounts.invite_lecturer(user.institution_id, body.email, body.full_name))
        except AccountError as e:
            raise _http(e)

    @app.patch("/admin/lecturers/{lecturer_id}")
    async def patch_lecturer(lecturer_id: uuid.UUID, body: LecturerPatch, user: AuthUser = Depends(admin_only)):
        row = await accounts.update_lecturer(user.institution_id, lecturer_id, is_active=body.is_active, full_name=body.full_name)
        if row is None:
            raise HTTPException(404, "Lecturer not found.")
        return _rec(row)

    @app.post("/admin/lecturers/{lecturer_id}/resend-invite", status_code=202)
    async def resend_invite(lecturer_id: uuid.UUID, user: AuthUser = Depends(admin_only)):
        try:
            await accounts.resend_invite(user.institution_id, lecturer_id)
        except AccountError as e:
            raise _http(e)
        return {"message": "Invite sent."}

    @app.get("/admin/usage")
    async def usage(user: AuthUser = Depends(admin_only)):
        u = await db.get_usage(user.institution_id)
        return {"used_seconds": u.used_seconds, "limit_seconds": u.limit_seconds, "remaining_seconds": u.remaining_seconds}

    @app.get("/admin/sessions")
    async def sessions(limit: int = Query(50, ge=1, le=200), user: AuthUser = Depends(admin_only)):
        return [_rec(r) for r in await accounts.recent_sessions(user.institution_id, limit)]

    @app.get("/admin/email-domains")
    async def domains(user: AuthUser = Depends(admin_only)):
        return [_rec(r) for r in await accounts.list_domains(user.institution_id)]

    # ---- rooms (lecturers and the admin) -----------------------------------------------------------------------
    async def own_room(user: AuthUser, room_id: uuid.UUID):
        room = await accounts.get_room(user.institution_id, room_id)
        if room is None:
            raise HTTPException(404, "Room not found.")
        if user.role != "ADMIN" and room["assigned_lecturer_id"] != user.id:
            raise HTTPException(403, "Only the lecturer who owns this room, or the school admin, can change it.")
        return room

    @app.get("/rooms")
    async def list_rooms(user: AuthUser = Depends(staff)):
        return [{**_rec(r), "mine": r["assigned_lecturer_id"] == user.id} for r in await accounts.list_rooms(user.institution_id)]

    @app.post("/rooms", status_code=201)
    async def create_room(body: RoomIn, user: AuthUser = Depends(staff)):
        try:
            room = await accounts.create_room(user.institution_id, user.id, body.room_code, body.room_name)
        except AccountError as e:
            raise _http(e)
        return {"id": str(room.id), "room_code": room.room_code, "room_name": body.room_name, "join_code": room.join_code}

    @app.patch("/rooms/{room_id}")
    async def patch_room(room_id: uuid.UUID, body: RoomPatch, user: AuthUser = Depends(staff)):
        await own_room(user, room_id)
        return _rec(await accounts.update_room(user.institution_id, room_id, body.room_name))

    @app.post("/rooms/{room_id}/rotate-code")
    async def rotate_code(room_id: uuid.UUID, user: AuthUser = Depends(staff)):
        await own_room(user, room_id)
        return {"join_code": await accounts.rotate_join_code(user.institution_id, room_id)}

    # ---- students ----------------------------------------------------------------------------------------------------
    @app.get("/join/{code}")
    async def join_info(code: str, user: AuthUser = Depends(student_only)):
        target = await db.get_join_target(code)
        if target is None:
            raise HTTPException(404, "That room code wasn't found.")
        if target.room.institution_id != user.institution_id:
            raise HTTPException(403, "That room belongs to a different school.")
        room = await accounts.get_room(user.institution_id, target.room.id)
        return {"room_code": target.room.room_code, "room_name": room["room_name"], "is_live": target.session is not None}

    app.state.ctx = ctx
    return app
