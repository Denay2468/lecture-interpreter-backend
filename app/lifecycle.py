"""Session lifecycle: starts and ends lecture sessions in the database, keeps
them alive with heartbeats, and cleans up after crashes.

Metering lives here. A session's billable time is wall-clock time between the
row being created (the lecturer connecting) and the lecturer's connection
ending, measured by the database clock, not this server's.

Failure policy: fail closed when STARTING (no database means no quota check, so
no lecture), but fail open once a lecture is RUNNING. A heartbeat or completion
that errors is logged and retried or reaped later; it never touches the audio
pipeline.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from .db import Database, LiveSession

logger = logging.getLogger("lifecycle")


class SessionLifecycle:
    def __init__(self, db: Database, heartbeat_interval_s: float = 30.0, stale_after_s: float = 90.0) -> None:
        self._db = db
        self._interval = heartbeat_interval_s
        self._stale_after = stale_after_s
        self._beats: dict[uuid.UUID, asyncio.Task] = {}
        self._reaper: Optional[asyncio.Task] = None

    async def start_session(self, room_id: uuid.UUID, lecturer_id: uuid.UUID,
                            source_language: str, target_language: str) -> LiveSession:
        session = await self._db.start_session(room_id, lecturer_id, source_language, target_language)
        self._beats[session.id] = asyncio.create_task(self._beat(session.id))
        logger.info("Session %s started (room %s, %s -> %s)", session.id, room_id, source_language, target_language)
        return session

    async def end_session(self, session_id: uuid.UUID, reason: str) -> Optional[int]:
        """Stops the heartbeat and records the billable seconds. Safe to call
        twice; returns None if already ended or if the database is unreachable
        (in which case the reaper settles the session later)."""
        task = self._beats.pop(session_id, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            duration = await self._db.complete_session(session_id, reason)
        except Exception:  # noqa: BLE001 - the lecture is over; don't let bookkeeping raise into cleanup
            logger.exception("Could not complete session %s; it will be reaped after missing heartbeats", session_id)
            return None
        if duration is None:
            logger.info("Session %s was already ended", session_id)
        else:
            logger.info("Session %s completed (%s): %ds billable", session_id, reason, duration)
        return duration

    async def recover_on_startup(self) -> int:
        """Settles sessions a previous process left LIVE. Assumes a single
        server instance; with several, use heartbeat-based reaping only."""
        aborted = await self._db.abort_stale_sessions(0, reason="server_restart")
        for sid, duration in aborted:
            logger.warning("Session %s was left LIVE by a previous run; aborted at %ds", sid, duration)
        return len(aborted)

    def start_reaper(self) -> None:
        self._reaper = asyncio.create_task(self._reap_loop())

    async def stop(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            try:
                await self._reaper
            except asyncio.CancelledError:
                pass
            self._reaper = None
        for session_id in list(self._beats):
            await self.end_session(session_id, "server_shutdown")

    async def _beat(self, session_id: uuid.UUID) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                alive = await self._db.heartbeat(session_id)
            except Exception:  # noqa: BLE001
                logger.warning("Heartbeat for session %s failed; will retry", session_id, exc_info=True)
                continue
            if not alive:
                logger.warning("Session %s is no longer LIVE in the database (reaped?); stopping its heartbeat", session_id)
                return

    async def _reap_loop(self) -> None:
        while True:
            try:
                for sid, duration in await self._db.abort_stale_sessions(self._stale_after):
                    logger.warning("Session %s stopped heartbeating; aborted at %ds", sid, duration)
            except Exception:  # noqa: BLE001
                logger.warning("Stale-session reaper failed; will retry", exc_info=True)
            await asyncio.sleep(self._interval)
