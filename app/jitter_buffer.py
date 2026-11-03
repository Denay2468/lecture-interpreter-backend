"""Jitter Buffer — sits between the Realtime Transport Layer and
Segmentation on the teacher audio path (architecture spec Section 1).

Smooths out network-induced burstiness (packet delay variance, bursty
re-delivery) by reordering and pacing incoming audio frames before they
reach segmentation, so network jitter is never misread as a real speech
pause. See the architecture spec's Failure Handling section (7) for how
this relates to the "audio jitter / packet bursts" row.
"""
from __future__ import annotations

import asyncio
import heapq
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .protocol import AudioFrame


@dataclass(order=True)
class _BufferedFrame:
    sequence: int
    arrival_time: float = field(compare=False)
    frame: AudioFrame = field(compare=False)


GapCallback = Callable[[int, int], None]  # (missing_from_seq, missing_to_seq) -> None


class JitterBuffer:
    """A deliberately simple MVP jitter buffer — not a full RTP-style
    implementation, but enough to satisfy what the architecture spec
    actually asks for:

      - Reorders frames that arrive out of sequence, within a bounded window.
      - Holds each frame for `target_delay_ms` after arrival before
        releasing it, giving slightly-late/out-of-order frames a chance
        to catch up and be placed correctly.
      - If a gap (missing sequence number) persists past `max_wait_ms`,
        gives up waiting and releases what it has. A jitter buffer that
        waits forever for one lost packet would itself become exactly
        the kind of blocking behavior the architecture explicitly avoids
        everywhere else (see Rule 6 / the Queue Behavior section: the
        pipeline should never stall indefinitely over one bad segment).
    """

    def __init__(
        self,
        target_delay_ms: int,
        max_wait_ms: int,
        tick_ms: int,
        on_gap: Optional[GapCallback] = None,
    ) -> None:
        self._target_delay_s = target_delay_ms / 1000.0
        self._max_wait_s = max_wait_ms / 1000.0
        self._tick_s = tick_ms / 1000.0
        self._on_gap = on_gap

        self._heap: list[_BufferedFrame] = []
        self._next_expected_seq: Optional[int] = None
        self._expecting_since: Optional[float] = None

        self._output: asyncio.Queue[AudioFrame] = asyncio.Queue()
        self._release_task: Optional[asyncio.Task] = None
        self._closed = False

        # Metrics — useful once you have real network conditions to tune
        # target_delay_ms / max_wait_ms against (see architecture spec's
        # note on testing transport jitter early rather than late).
        self.frames_received = 0
        self.frames_released = 0
        self.frames_dropped_stale = 0
        self.frames_lost_to_gap = 0

    def start(self) -> None:
        """Starts the background release loop. Call once per teacher session."""
        if self._release_task is None:
            self._release_task = asyncio.create_task(self._release_loop())

    async def stop(self) -> None:
        self._closed = True
        if self._release_task is not None:
            self._release_task.cancel()
            try:
                await self._release_task
            except asyncio.CancelledError:
                pass
        # Without this, any frame still sitting in the buffer waiting out
        # its jitter-absorption window (or a gap's grace period) at the
        # moment the session ends would be silently discarded rather than
        # delivered — a real bug this slice's own smoke test caught (the
        # debug WAV came out ~0.5s shorter than the input for reasons
        # beyond the simulated packet loss).
        self.flush()

    def flush(self) -> None:
        """Immediately releases all remaining buffered frames, in
        sequence order, ignoring any further wait windows. Called on
        shutdown so in-flight audio isn't lost."""
        for buffered in sorted(self._heap, key=lambda b: b.sequence):
            self._output.put_nowait(buffered.frame)
            self.frames_released += 1
        self._heap.clear()

    def pending_count(self) -> int:
        """How many smoothed frames are sitting in the output queue,
        not yet consumed. Used by TeacherAudioIngest to fully drain the
        queue on shutdown before tearing down its consumer task."""
        return self._output.qsize()

    def push(self, frame: AudioFrame) -> None:
        """Called by Teacher Audio Ingest whenever a new frame arrives
        from the transport layer. Non-blocking — safe to call from the
        WebSocket receive loop directly."""
        self.frames_received += 1
        if self._next_expected_seq is None:
            self._next_expected_seq = frame.sequence

        heapq.heappush(
            self._heap,
            _BufferedFrame(sequence=frame.sequence, arrival_time=time.monotonic(), frame=frame),
        )

    async def get(self) -> AudioFrame:
        """Consumed by Teacher Audio Ingest to read the smoothed,
        in-order output stream — this is what Segmentation will
        eventually read from in the next build phase."""
        return await self._output.get()

    async def _release_loop(self) -> None:
        try:
            while not self._closed:
                self._release_ready_frames()
                await asyncio.sleep(self._tick_s)
        except asyncio.CancelledError:
            raise

    def _release_ready_frames(self) -> None:
        now = time.monotonic()

        while self._heap:
            top = self._heap[0]

            # Stale/duplicate frame: sequence we've already moved past. Discard.
            if self._next_expected_seq is not None and top.sequence < self._next_expected_seq:
                heapq.heappop(self._heap)
                self.frames_dropped_stale += 1
                continue

            if top.sequence == self._next_expected_seq:
                if now - top.arrival_time >= self._target_delay_s:
                    heapq.heappop(self._heap)
                    self._output.put_nowait(top.frame)
                    self.frames_released += 1
                    self._next_expected_seq = top.sequence + 1
                    self._expecting_since = None
                    continue
                else:
                    break  # still within its jitter-absorption window; wait for next tick

            # top.sequence > next_expected_seq: there's a gap.
            if self._expecting_since is None:
                self._expecting_since = now

            if now - self._expecting_since >= self._max_wait_s:
                missing_from = self._next_expected_seq
                missing_to = top.sequence - 1
                self.frames_lost_to_gap += missing_to - missing_from + 1
                if self._on_gap:
                    self._on_gap(missing_from, missing_to)
                self._next_expected_seq = top.sequence  # give up, jump forward
                self._expecting_since = None
                continue  # re-check: top now matches next_expected_seq

            break  # still within the gap's grace period; wait for next tick
