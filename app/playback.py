"""Ordered Student Playback — architecture spec Section 1/2.

The Translation Queue runs multiple concurrent workers (Section 4), so
translated segments can finish out of the order they were originally
spoken in — segment #2 might complete before segment #1 if #1 hits a
slower API response. This module reorders results back into strict
speaking order before anything reaches the student.

This is made tractable by a deliberate design decision made alongside
this module (see translation_pipeline.py): every segment, including
pure-silence ones with nothing to translate, always produces exactly
one result. That guarantees this buffer will eventually see every
sequence number with no permanent gaps — unlike app/jitter_buffer.py,
which genuinely can lose network packets forever and needs a
give-up-and-move-on timeout, this buffer never needs one: it can always
just wait for the next sequence number, because it's guaranteed to
eventually arrive.
"""
from __future__ import annotations

import asyncio
import heapq
import logging
from dataclasses import dataclass, field

from .translation_pipeline import TranslatedSegment

logger = logging.getLogger("playback")


@dataclass(order=True)
class _PendingResult:
    sequence: int
    result: TranslatedSegment = field(compare=False)


class StudentPlaybackController:
    """One instance per teacher session.

    Consumes (possibly out-of-order) TranslatedSegment results via
    `submit()`, and exposes a strictly in-order stream of playable
    (delivered) segments via `get_next()`. Skipped/silent segments are
    absorbed internally to keep the sequence moving — there's nothing
    to play for them, but they still have to "arrive" before later
    segments can be released.
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._heap: list[_PendingResult] = []
        self._next_expected_sequence = 0
        self._playable: asyncio.Queue[TranslatedSegment] = asyncio.Queue()

        self.segments_played = 0
        self.segments_silently_advanced = 0

    def submit(self, result: TranslatedSegment) -> None:
        """Wired as TranslationQueue's on_translated callback (usually
        composed with the debug writer — see session.py). Safe to call
        with results arriving in any order; non-blocking."""
        heapq.heappush(self._heap, _PendingResult(sequence=result.sequence, result=result))
        self._release_ready()

    def _release_ready(self) -> None:
        while self._heap and self._heap[0].sequence == self._next_expected_sequence:
            pending = heapq.heappop(self._heap)
            result = pending.result
            self._next_expected_sequence += 1

            if result.state == "delivered" and result.audio:
                self._playable.put_nowait(result)
                self.segments_played += 1
            else:
                self.segments_silently_advanced += 1
                logger.debug(
                    "Session %s segment #%d not played (state=%s)",
                    self._session_id, result.sequence, result.state,
                )

    async def get_next(self) -> TranslatedSegment:
        """Consumed by the /ws/student route: awaits the next segment
        that's actually ready to play, in strict original speaking
        order — regardless of the order the Translation Queue's
        workers happened to finish them in."""
        return await self._playable.get()

    def pending_count(self) -> int:
        return self._playable.qsize()
