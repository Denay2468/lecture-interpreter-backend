"""Realtime Transport Layer.

A plain asyncio WebSocket server. Two modes:

  Single-session mode (no DATABASE_URL): one shared session, no accounts, no
  metering. This is what the local test scripts use.

  Multi-tenant mode (DATABASE_URL set): every lecture is a row in PostgreSQL.
    ws://host:port/ws/teacher?room=<room label>&token=<JWT>[&source=id&target=en]
        The token identifies the lecturer; `room` is the room's label (e.g.
        CS-302) within the lecturer's own institution. The lecture is started
        in the database first (quota, subscription and one-live-lecture-per-room
        checks) and only then is the speech pipeline brought up, so a refused
        lecture never opens a Deepgram connection.
    ws://host:port/ws/student?code=<join code>&token=<student access token>
        Students sign in with their university email like everyone else. The
        token must be a STUDENT's, the code must belong to a room at the
        student's own school, and that room must have a live lecture.

  Both routes carry audio and JSON control messages in both directions (see
  protocol.py and session.py).

Refusals are WebSocket close codes in the application range, with a short
human-readable reason:
    4400 bad request        4401 unauthorized      4403 forbidden (subscription, quota, not allowed)
    4404 unknown room/code  4409 conflict (room busy / a student already connected)
    4412 no lecture live (retry shortly)           4413 the lecture ended
    1011 server error (e.g. database unavailable, speech pipeline failed to start)

The database is never in the audio path: it is used when a lecture starts and
ends and for a periodic heartbeat (lifecycle.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

import websockets
from websockets.server import WebSocketServerProtocol

from .auth import AuthError, TEACHING_ROLES, verify_access_token
from .config import Config
from .db import (Database, InstitutionInactive, LecturerNotAllowed, LiveSession,
                 QuotaExceeded, RoomBusy, RoomNotFound)
from .lifecycle import SessionLifecycle
from .protocol import encode_playback_frame
from .session import SessionCoordinator

logger = logging.getLogger("server")

CLOSE_BAD_REQUEST = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404
CLOSE_CONFLICT = 4409
CLOSE_NOT_LIVE = 4412
CLOSE_LECTURE_ENDED = 4413
CLOSE_SERVER_ERROR = 1011

# The speech pipeline is wired for Indonesian -> English only.
SUPPORTED_LANGUAGE_PAIRS = {("id", "en")}
LEGACY_KEY = "legacy"


@dataclass
class ActiveSession:
    key: object                     # room id (multi-tenant) or LEGACY_KEY
    session_id: str
    coordinator: SessionCoordinator
    student_ws: Optional[WebSocketServerProtocol] = None


class Server:
    def __init__(self, cfg: Config, db: Optional[Database] = None, coordinator_factory=SessionCoordinator) -> None:
        self._cfg = cfg
        self._db = db
        self._coordinator_factory = coordinator_factory
        self._active: dict = {}
        self._lifecycle: Optional[SessionLifecycle] = None
        if db is not None:
            if not cfg.jwt_secret:
                raise RuntimeError("JWT_SECRET must be set when DATABASE_URL is configured")
            self._lifecycle = SessionLifecycle(db, cfg.heartbeat_interval_s, cfg.stale_session_after_s)
        else:
            self._active[LEGACY_KEY] = ActiveSession(LEGACY_KEY, "legacy", coordinator_factory(cfg))

    @staticmethod
    async def _reject(websocket: WebSocketServerProtocol, code: int, reason: str) -> None:
        logger.info("Rejecting connection (%d): %s", code, reason)
        await websocket.close(code=code, reason=reason[:120])

    # ---------------------------------------------------------------- teacher

    async def _handle_teacher(self, websocket: WebSocketServerProtocol, params: dict) -> None:
        if self._db is None:
            active = self._active[LEGACY_KEY]
            if active.coordinator.teacher_connected:
                await self._reject(websocket, CLOSE_CONFLICT, "A teacher is already connected")
                return
            session_id = params.get("session_id", [str(uuid.uuid4())])[0]
            active.session_id = session_id
        else:
            live = await self._start_db_session(websocket, params)
            if live is None:
                return
            session_id = str(live.id)
            active = ActiveSession(live.room_id, session_id, self._coordinator_factory(self._cfg))
            self._active[live.room_id] = active

        coordinator = active.coordinator
        logger.info("Teacher connected: session_id=%s", session_id)
        try:
            await coordinator.attach_teacher(session_id)
        except Exception as e:  # noqa: BLE001 - e.g. no Deepgram key, or the connection failed
            logger.error("Could not attach teacher session %s: %s", session_id, e)
            await self._finish_teacher(active, "attach_failed")
            await self._reject(websocket, CLOSE_SERVER_ERROR, str(e))
            return

        async def send_audio(audio: bytes) -> None:
            # No sequence header: at most one question is in flight at a time (Rule 1).
            try:
                await websocket.send(audio)
            except websockets.ConnectionClosed:
                pass

        async def send_control(message: dict) -> None:
            try:
                await websocket.send(json.dumps(message))
            except websockets.ConnectionClosed:
                pass

        coordinator.register_teacher_sender(send_audio, send_control)
        # Rule 11: a teacher joining mid-question learns the mode immediately.
        await send_control({"type": "mode_changed", "mode": coordinator.current_mode})

        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    coordinator.handle_teacher_frame(session_id, message)
                else:
                    logger.debug("Ignoring non-binary message from teacher %s: %r", session_id, message)
        except websockets.ConnectionClosed:
            pass
        finally:
            await self._finish_teacher(active, "teacher_disconnected")
            logger.info("Teacher disconnected: session_id=%s", session_id)

    async def _start_db_session(self, websocket: WebSocketServerProtocol, params: dict) -> Optional[LiveSession]:
        """Authenticates the lecturer and starts the lecture in the database.
        Returns None after closing the connection with the right code."""
        def first(name: str, default: str = "") -> str:
            return params.get(name, [default])[0]

        pair = (first("source", "id"), first("target", "en"))
        if pair not in SUPPORTED_LANGUAGE_PAIRS:
            await self._reject(websocket, CLOSE_BAD_REQUEST, "Unsupported language pair")
            return None
        try:
            claims = verify_access_token(first("token"), self._cfg.jwt_secret)
        except AuthError:
            await self._reject(websocket, CLOSE_UNAUTHORIZED, "Invalid or expired token")
            return None
        try:
            user = await self._db.get_active_user(claims.user_id)
            room = (await self._db.get_room_by_label(user.institution_id, first("room"))
                    if user and user.role in TEACHING_ROLES else None)
        except Exception:  # noqa: BLE001
            logger.exception("Database error while starting a lecture")
            await self._reject(websocket, CLOSE_SERVER_ERROR, "Service temporarily unavailable")
            return None
        if user is None or user.role != claims.role:
            await self._reject(websocket, CLOSE_UNAUTHORIZED, "Your session is no longer valid. Sign in again.")
            return None
        if user.role not in TEACHING_ROLES:
            await self._reject(websocket, CLOSE_FORBIDDEN, "Only lecturers can start a lecture")
            return None
        if room is None:
            await self._reject(websocket, CLOSE_NOT_FOUND, "Unknown room")
            return None
        try:
            return await self._lifecycle.start_session(room.id, user.id, *pair)
        except RoomBusy as e:
            await self._reject(websocket, CLOSE_CONFLICT, str(e))
        except (QuotaExceeded, InstitutionInactive, LecturerNotAllowed) as e:
            await self._reject(websocket, CLOSE_FORBIDDEN, str(e))
        except RoomNotFound as e:
            await self._reject(websocket, CLOSE_NOT_FOUND, str(e))
        except Exception:  # noqa: BLE001
            logger.exception("Database error while starting a lecture")
            await self._reject(websocket, CLOSE_SERVER_ERROR, "Service temporarily unavailable")
        return None

    async def _finish_teacher(self, active: ActiveSession, reason: str) -> None:
        coordinator = active.coordinator
        playback = coordinator.current_playback
        multi_tenant = active.key != LEGACY_KEY
        if multi_tenant and self._lifecycle is not None:
            # Stop the billing clock first: draining the translation queue below
            # can take seconds and is not lecture time.
            await self._lifecycle.end_session(uuid.UUID(active.session_id), reason)
        coordinator.unregister_teacher_sender()
        try:
            await coordinator.detach_teacher(active.session_id)
        except Exception:  # noqa: BLE001
            logger.exception("Error while detaching teacher session %s", active.session_id)
        if not multi_tenant:
            return
        # Only remove our own entry: a new lecture may already have started in this room.
        if self._active.get(active.key) is active:
            del self._active[active.key]
        student = active.student_ws
        if student is not None:
            await self._drain_playback(playback)
            await student.close(code=CLOSE_LECTURE_ENDED, reason="The lecture has ended")

    @staticmethod
    async def _drain_playback(playback) -> None:
        """Lets already-translated segments reach the student before the connection closes."""
        if playback is not None:
            for _ in range(30):
                if playback.pending_count() == 0:
                    break
                await asyncio.sleep(0.1)
        await asyncio.sleep(0.3)

    # ---------------------------------------------------------------- student

    async def _handle_student(self, websocket: WebSocketServerProtocol, params: dict) -> None:
        if self._db is None:
            active = self._active[LEGACY_KEY]
        else:
            active = await self._resolve_student_session(websocket, params)
            if active is None:
                return
        if active.coordinator.has_student:
            await self._reject(websocket, CLOSE_CONFLICT, "A student is already connected (one per room for now)")
            return
        await self._serve_student(websocket, active)

    async def _resolve_student_session(self, websocket: WebSocketServerProtocol, params: dict) -> Optional[ActiveSession]:
        # Who you are is settled before anything about the room is revealed.
        try:
            claims = verify_access_token(params.get("token", [""])[0], self._cfg.jwt_secret)
        except AuthError:
            await self._reject(websocket, CLOSE_UNAUTHORIZED, "Sign in to join a lecture")
            return None
        try:
            user = await self._db.get_active_user(claims.user_id)
            target = await self._db.get_join_target(params.get("code", [""])[0]) if user else None
        except Exception:  # noqa: BLE001
            logger.exception("Database error while resolving a join code")
            await self._reject(websocket, CLOSE_SERVER_ERROR, "Service temporarily unavailable")
            return None
        if user is None or user.role != claims.role:
            await self._reject(websocket, CLOSE_UNAUTHORIZED, "Your session is no longer valid. Sign in again.")
            return None
        if user.role != "STUDENT":
            await self._reject(websocket, CLOSE_FORBIDDEN, "Only students can join as a student")
            return None
        if target is None:
            await self._reject(websocket, CLOSE_NOT_FOUND, "Unknown room code")
            return None
        if target.room.institution_id != user.institution_id:
            await self._reject(websocket, CLOSE_FORBIDDEN, "That room belongs to a different school")
            return None
        if target.session is None:
            await self._reject(websocket, CLOSE_NOT_LIVE, "No lecture is live in this room yet")
            return None
        active = self._active.get(target.room.id)
        if active is None or active.session_id != str(target.session.id):
            # LIVE in the database but not held by this process (e.g. another instance).
            await self._reject(websocket, CLOSE_NOT_LIVE, "This lecture is not available on this server")
            return None
        return active

    async def _serve_student(self, websocket: WebSocketServerProtocol, active: ActiveSession) -> None:
        coord = active.coordinator
        logger.info("Student connected")
        active.student_ws = websocket

        async def send_control(message: dict) -> None:
            try:
                await websocket.send(json.dumps(message))
            except websockets.ConnectionClosed:
                pass

        # Registered immediately so presses and question audio are handled in real time.
        coord.register_student_sender(send_control)
        await send_control({"type": "mode_changed", "mode": coord.current_mode})

        async def wait_for_question_mode() -> None:
            while not coord.is_question_mode:
                await asyncio.sleep(0.05)

        async def send_loop() -> None:
            controller = coord.current_playback
            if controller is None:
                logger.info("Student waiting for the lecture's speech pipeline to finish starting...")
            while controller is None:
                await asyncio.sleep(0.2)
                controller = coord.current_playback
            logger.info("Student wired to the active teacher session")

            # Rule 5: pause dequeuing (not processing) during Question Mode.
            while True:
                await coord.lecture_mode_event.wait()
                get_task = asyncio.create_task(controller.get_next())
                question_task = asyncio.create_task(wait_for_question_mode())
                done, pending = await asyncio.wait({get_task, question_task}, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                if get_task in done:
                    result = get_task.result()
                    # wait_for_question_mode polls every 50ms, so a segment landing right
                    # after a press can win the race and arrive here during Question Mode.
                    # Hold it (already dequeued; this loop is sequential, so order is kept).
                    await coord.lecture_mode_event.wait()
                    # Text first, then audio: a UI can show the caption as the audio starts
                    # (the two are paired by `sequence`, which the audio frame also carries).
                    await websocket.send(json.dumps({
                        "type": "segment", "sequence": result.sequence,
                        "source_transcript": result.source_transcript, "translated_text": result.translated_text}))
                    await websocket.send(encode_playback_frame(result.sequence, result.audio))
                    logger.info("Sent segment #%d to student (%d bytes): %r -> %r", result.sequence,
                                len(result.audio), result.source_transcript, result.translated_text)
                else:
                    try:
                        await get_task
                    except asyncio.CancelledError:
                        pass

        async def recv_loop() -> None:
            async for message in websocket:
                if isinstance(message, bytes):
                    coord.handle_question_audio_frame(message)
                    continue
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning("Malformed control message from student: %r", message)
                    continue
                msg_type = data.get("type")
                if msg_type == "ask_press":
                    await coord.handle_ask_press()
                elif msg_type == "ask_release":
                    await coord.handle_ask_release()
                else:
                    logger.debug("Unknown control message type from student: %r", msg_type)

        send_task = asyncio.create_task(send_loop())
        recv_task = asyncio.create_task(recv_loop())
        try:
            await asyncio.wait({send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED)
        except websockets.ConnectionClosed:
            pass
        finally:
            send_task.cancel()
            recv_task.cancel()
            for t in (send_task, recv_task):
                try:
                    await t
                except (asyncio.CancelledError, websockets.ConnectionClosed):
                    pass
            coord.unregister_student_sender()
            await coord.handle_student_disconnect()
            active.student_ws = None
            logger.info("Student disconnected")

    # ---------------------------------------------------------------- plumbing

    async def _router(self, websocket: WebSocketServerProtocol) -> None:
        # websockets >= 13 passes only `websocket`; the path is websocket.request.path.
        parsed = urlparse(websocket.request.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/ws/teacher":
            await self._handle_teacher(websocket, params)
        elif parsed.path == "/ws/student":
            await self._handle_student(websocket, params)
        else:
            await self._reject(websocket, 1008, f"Unknown route: {parsed.path}")

    async def serve_forever(self) -> None:
        if self._lifecycle is not None:
            await self._lifecycle.recover_on_startup()
            self._lifecycle.start_reaper()
        try:
            async with websockets.serve(self._router, self._cfg.host, self._cfg.port, max_size=None):
                logger.info("Listening on ws://%s:%d (%s)", self._cfg.host, self._cfg.port,
                            "multi-tenant, PostgreSQL" if self._db is not None else "single-session, no database")
                logger.info("Teacher endpoint: /ws/teacher?room=<label>&token=<JWT>" if self._db is not None
                            else "Teacher endpoint: /ws/teacher?session_id=<id>")
                logger.info("Student endpoint: /ws/student?code=<join code>&token=<JWT>" if self._db is not None
                            else "Student endpoint: /ws/student")
                await asyncio.Future()  # run forever
        finally:
            if self._lifecycle is not None:
                await self._lifecycle.stop()
