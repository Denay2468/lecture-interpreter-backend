"""Segmentation — architecture spec Section 1/2/8 ("hybrid trigger").

Consumes the jitter-smoothed audio stream (via the `handle_frame`
method, wired as TeacherAudioIngest's `on_smoothed_frame` callback) and
cuts it into discrete Segments using two signals from Deepgram's
streaming STT, matching the spec's "STT completion primary, VAD silence
fallback" design:

  PRIMARY trigger  -> Deepgram's `speech_final` flag on a Results
                       message. This fires based on Deepgram's own
                       endpointing (informed by more than raw silence
                       duration), so it's the closer match to "the STT
                       service's own utterance-completion signal."

  FALLBACK trigger -> Deepgram's `UtteranceEnd` event. A separate,
                       longer silence timer that Deepgram itself
                       describes as a safety net for when endpointing
                       doesn't fire — this is the VAD-silence backstop
                       the spec calls for.

  HARD CAP (added here, not in the original spec) -> a maximum segment
                       duration, so one segment can never grow
                       unboundedly if neither of the above fires.

Design note: the boundary-triggering logic (`_on_stt_event` /
`_close_segment`) is deliberately kept separate from the Deepgram
connection plumbing (`start` / `_on_dg_message`), so it can be unit
tested with fake events with no network involved — see
tools/test_segmenter_logic.py, which does exactly that.
"""
from __future__ import annotations

import asyncio
import logging
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

from .config import Config

logger = logging.getLogger("segmentation")


@dataclass(frozen=True)
class Segment:
    session_id: str
    sequence: int
    audio: bytes            # raw PCM16LE payload for this segment
    transcript: str
    trigger: str             # "speech_final" | "utterance_end" | "max_duration" | "session_end"
    duration_ms: float


OnSegment = Callable[[Segment], Union[None, Awaitable[None]]]


def _now_ms() -> float:
    return time.monotonic() * 1000.0


class Segmenter:
    """One instance per teacher session."""

    def __init__(
        self,
        cfg: Config,
        session_id: str,
        on_segment: Optional[OnSegment] = None,
    ) -> None:
        self._cfg = cfg
        self._session_id = session_id
        self._using_default_sink = on_segment is None
        self._on_segment = on_segment or self._default_debug_sink

        # Current in-progress segment state.
        self._current_audio = bytearray()
        self._current_transcript_parts: list[str] = []
        self._segment_start_ms: Optional[float] = None
        self._segment_index = 0

        # Deepgram connection plumbing (set up in start()).
        self._dg_client = None
        self._dg_connection = None
        self._listen_task: Optional[asyncio.Task] = None

        self.closed = False

    # ---------------------------------------------------------------
    # Deepgram-specific transport. Kept separate from the boundary
    # logic below on purpose (see module docstring).
    # ---------------------------------------------------------------

    async def start(self) -> None:
        if not self._cfg.deepgram_api_key:
            raise RuntimeError("DEEPGRAM_API_KEY not set — cannot start Segmentation")

        from deepgram import AsyncDeepgramClient
        from deepgram.core.events import EventType

        self._dg_client = AsyncDeepgramClient(api_key=self._cfg.deepgram_api_key)

        self._dg_connection_ctx = self._dg_client.listen.v1.connect(
            model=self._cfg.deepgram_model,
            language=self._cfg.segmentation_language,
            smart_format=True,
            interim_results=True,
            endpointing=self._cfg.segmentation_endpointing_ms,
            utterance_end_ms=str(self._cfg.segmentation_utterance_end_ms),
            vad_events=True,
            encoding="linear16",
            sample_rate=self._cfg.sample_rate_hz,
            channels=self._cfg.channels,
        )
        self._dg_connection = await self._dg_connection_ctx.__aenter__()
        self._dg_connection.on(EventType.MESSAGE, self._on_dg_message)
        self._listen_task = asyncio.create_task(self._dg_connection.start_listening())
        logger.info("Segmentation started for session %s (model=%s, language=%s)",
                    self._session_id, self._cfg.deepgram_model, self._cfg.segmentation_language)

    async def stop(self) -> None:
        self.closed = True
        # Flush whatever's left as a final segment before tearing down.
        if self._current_audio:
            await self._close_segment(trigger="session_end")

        if self._dg_connection is not None:
            try:
                await self._dg_connection.send_close_stream()
            except Exception:  # noqa: BLE001 -- best-effort during shutdown
                pass
        if self._listen_task is not None:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if getattr(self, "_dg_connection_ctx", None) is not None:
            try:
                await self._dg_connection_ctx.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        logger.info("Segmentation stopped for session %s (%d segments produced)",
                    self._session_id, self._segment_index)

    async def force_close(self) -> None:
        """Immediately finalizes whatever segment is currently in
        progress, without waiting for speech_final or UtteranceEnd.

        Used when the teacher is interrupted by a student question
        (Rule 3: in-flight teacher audio is never discarded). Without
        this, the last segment before an interruption would just sit
        waiting for Deepgram's own silence timeout to notice that no
        more audio is coming — technically it would still eventually
        close and get delivered, but there's no reason to wait for a
        timeout to fire over audio that's already fully arrived and
        whose capture has already stopped on purpose.
        """
        if self._current_audio:
            await self._close_segment(trigger="interrupted")

    async def handle_frame(self, frame) -> None:
        """Wired as TeacherAudioIngest's on_smoothed_frame callback.
        `frame` is an AudioFrame (see protocol.py) — only .payload is
        used here."""
        if self._segment_start_ms is None:
            self._segment_start_ms = _now_ms()

        self._current_audio.extend(frame.payload)

        if self._dg_connection is not None:
            await self._dg_connection.send_media(frame.payload)

        # Hard cap safety net — see module docstring.
        if _now_ms() - self._segment_start_ms >= self._cfg.segmentation_max_duration_ms:
            await self._close_segment(trigger="max_duration")

    async def _on_dg_message(self, message) -> None:
        msg_type = getattr(message, "type", None)
        if msg_type == "Results":
            is_final = bool(getattr(message, "is_final", False))
            speech_final = bool(getattr(message, "speech_final", False))
            transcript = _extract_transcript(message)
            if is_final:
                # Diagnostic: in both real runs so far, segments closed via
                # the UtteranceEnd fallback rather than speech_final. Logging
                # every finalized chunk with its flag shows whether
                # speech_final is genuinely never arriving, or arriving in a
                # shape this code doesn't handle. Low volume — finalized
                # chunks only, not interim drafts.
                logger.info(
                    "Session %s final chunk: %r (speech_final=%s)",
                    self._session_id, transcript, speech_final,
                )
            await self._on_stt_event(
                event="results",
                transcript=transcript,
                is_final=is_final,
                speech_final=speech_final,
            )
        elif msg_type == "UtteranceEnd":
            await self._on_stt_event(event="utterance_end", transcript="", is_final=True, speech_final=False)
        elif msg_type == "Error":
            logger.error("Deepgram error in session %s: %s", self._session_id, message)

    # ---------------------------------------------------------------
    # Boundary-triggering logic — transport-agnostic, unit-testable
    # without a real Deepgram connection (see
    # tools/test_segmenter_logic.py).
    # ---------------------------------------------------------------

    async def _on_stt_event(self, event: str, transcript: str, is_final: bool = True, speech_final: bool = False) -> None:
        # Deepgram sends interim (is_final=False) Results messages
        # repeatedly while it's still refining its guess at the same
        # stretch of speech — those are drafts, not confirmed text.
        # Accumulating them caused the same phrase to be appended
        # multiple times (caught via a real run — see the transcript
        # repetition bug this fixes). Only finalized chunks get kept.
        if event == "results" and is_final and transcript:
            self._current_transcript_parts.append(transcript)

        if event == "results" and speech_final and self._current_transcript_parts:
            await self._close_segment(trigger="speech_final")
        elif event == "utterance_end" and self._current_audio:
            await self._close_segment(trigger="utterance_end")

    async def _close_segment(self, trigger: str) -> None:
        if not self._current_audio:
            return

        duration_ms = _now_ms() - (self._segment_start_ms or _now_ms())
        segment = Segment(
            session_id=self._session_id,
            sequence=self._segment_index,
            audio=bytes(self._current_audio),
            transcript=" ".join(self._current_transcript_parts).strip(),
            trigger=trigger,
            duration_ms=duration_ms,
        )
        self._segment_index += 1
        self._current_audio = bytearray()
        self._current_transcript_parts = []
        self._segment_start_ms = None

        logger.info(
            "Session %s segment #%d closed (trigger=%s, %.0fms): %r",
            self._session_id, segment.sequence, segment.trigger, segment.duration_ms, segment.transcript,
        )

        result = self._on_segment(segment)
        if asyncio.iscoroutine(result):
            await result

    # ---------------------------------------------------------------
    # Default debug sink — writes each finalized segment to its own
    # WAV file plus logs its transcript, so this is testable before
    # the Translation Pipeline exists.
    # ---------------------------------------------------------------

    def _default_debug_sink(self, segment: Segment) -> None:
        out_dir = Path(self._cfg.debug_output_dir) / "segments"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{segment.session_id}_seg{segment.sequence:03d}_{segment.trigger}.wav"

        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(self._cfg.channels)
            wf.setsampwidth(self._cfg.bytes_per_sample)
            wf.setframerate(self._cfg.sample_rate_hz)
            wf.writeframes(segment.audio)

        transcript_path = path.with_suffix(".txt")
        transcript_path.write_text(segment.transcript, encoding="utf-8")


def _extract_transcript(message) -> str:
    try:
        return message.channel.alternatives[0].transcript or ""
    except (AttributeError, IndexError):
        return ""
