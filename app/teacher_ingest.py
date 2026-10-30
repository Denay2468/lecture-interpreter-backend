"""Teacher Audio Ingest — architecture spec Section 1/2.

Owns one JitterBuffer per teacher session. Responsibilities:
  1. Accept raw WebSocket binary messages from the transport layer,
     decode them with the wire protocol, and push them into the jitter
     buffer (non-blocking).
  2. Consume the jitter-smoothed output stream and forward each frame to
     a pluggable `on_smoothed_frame` callback.

That callback is the extension point for the next build phase —
Segmentation (hybrid STT-completion + VAD) reads from exactly this
point in the real system. This vertical slice ships a default debug
callback instead, so the pipeline is genuinely testable end-to-end
today: it writes the smoothed audio to a WAV file per session, so you
can listen to it and confirm frames arrived in order and nothing was
corrupted, and logs jitter buffer metrics when the session ends.
"""
from __future__ import annotations

import asyncio
import logging
import wave
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

from .config import Config
from .jitter_buffer import JitterBuffer
from .protocol import AudioFrame, ProtocolError, decode_frame

logger = logging.getLogger("teacher_ingest")

OnSmoothedFrame = Callable[[AudioFrame], Union[None, Awaitable[None]]]


class TeacherAudioIngest:
    """One instance per connected teacher session."""

    def __init__(
        self,
        cfg: Config,
        session_id: str,
        on_smoothed_frame: Optional[OnSmoothedFrame] = None,
    ) -> None:
        self._cfg = cfg
        self._session_id = session_id
        self._using_default_sink = on_smoothed_frame is None
        self._on_smoothed_frame = on_smoothed_frame or self._default_debug_sink

        self._jitter_buffer = JitterBuffer(
            target_delay_ms=cfg.jitter_target_delay_ms,
            max_wait_ms=cfg.jitter_max_wait_ms,
            tick_ms=cfg.jitter_tick_ms,
            on_gap=self._on_gap,
        )
        self._consumer_task: Optional[asyncio.Task] = None
        self._frames_forwarded = 0

        # Debug WAV sink state (used only by the default callback).
        self._debug_wav: Optional[wave.Wave_write] = None

    async def start(self) -> None:
        self._jitter_buffer.start()
        self._consumer_task = asyncio.create_task(self._consume_loop())
        if self._using_default_sink:
            self._open_debug_wav()

    async def stop(self) -> None:
        await self._jitter_buffer.stop()  # flushes remaining buffered frames into the output queue

        # Drain whatever the flush just produced before tearing down the
        # consumer, so audio that was still sitting in the jitter buffer
        # at shutdown still reaches the debug sink (or, later, Segmentation)
        # instead of being lost.
        while self._jitter_buffer.pending_count() > 0:
            frame = await self._jitter_buffer.get()
            self._frames_forwarded += 1
            result = self._on_smoothed_frame(frame)
            if asyncio.iscoroutine(result):
                await result

        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
        self._close_debug_wav()
        self._log_metrics()

    def handle_raw_message(self, raw: bytes) -> None:
        """Call this from the WebSocket receive loop for every incoming
        binary message. Decodes and pushes into the jitter buffer;
        never blocks, never awaits — safe to call directly from the
        recv loop without slowing down reception of the next frame."""
        try:
            frame = decode_frame(raw)
        except ProtocolError as e:
            logger.warning("Dropping malformed frame from session %s: %s", self._session_id, e)
            return
        self._jitter_buffer.push(frame)

    async def _consume_loop(self) -> None:
        while True:
            frame = await self._jitter_buffer.get()
            self._frames_forwarded += 1
            result = self._on_smoothed_frame(frame)
            if asyncio.iscoroutine(result):
                await result

    def _on_gap(self, missing_from: int, missing_to: int) -> None:
        count = missing_to - missing_from + 1
        logger.info(
            "Session %s: gave up waiting for %d frame(s) (seq %d-%d) after jitter_max_wait_ms",
            self._session_id, count, missing_from, missing_to,
        )

    # --- Default debug sink: writes smoothed audio to a WAV file so the
    # slice is testable before Segmentation exists. ---

    def _open_debug_wav(self) -> None:
        out_dir = Path(self._cfg.debug_output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{self._session_id}_smoothed.wav"
        self._debug_wav = wave.open(str(path), "wb")
        self._debug_wav.setnchannels(self._cfg.channels)
        self._debug_wav.setsampwidth(self._cfg.bytes_per_sample)
        self._debug_wav.setframerate(self._cfg.sample_rate_hz)
        logger.info("Session %s: writing smoothed audio to %s", self._session_id, path)

    def _default_debug_sink(self, frame: AudioFrame) -> None:
        if self._debug_wav is not None:
            self._debug_wav.writeframes(frame.payload)

    def _close_debug_wav(self) -> None:
        if self._debug_wav is not None:
            self._debug_wav.close()
            self._debug_wav = None

    def _log_metrics(self) -> None:
        jb = self._jitter_buffer
        logger.info(
            "Session %s jitter buffer summary: received=%d released=%d "
            "forwarded=%d dropped_stale=%d lost_to_gap=%d",
            self._session_id, jb.frames_received, jb.frames_released,
            self._frames_forwarded, jb.frames_dropped_stale, jb.frames_lost_to_gap,
        )
