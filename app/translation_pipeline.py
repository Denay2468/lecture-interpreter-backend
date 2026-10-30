"""Translation Queue + Pipeline — architecture spec Section 4 (Queue
Behavior) and Section 7 (Failure Handling).

Consumes finalized Segments (produced by Segmentation — STT already
happened, so this stage is just MT -> TTS) via `enqueue()`, wired as
Segmenter's `on_segment` callback.

Implements the queue model from the spec directly:
  - Strict FIFO. No segment is ever dropped for being "old" — see
    Rule 6 / Section 4. Backlog may grow; nothing gets silently evicted.
  - Failed lecture segments: retry once, then skip and continue
    (Rule 7 / Section 7). No user-facing error, no blocking the rest
    of the pipeline over one bad segment.
  - Processing is mode-agnostic — there's no Question Mode in this
    slice yet, but the queue's own worker loop has no dependency on
    anything except "is there a segment to process," matching the
    spec's design principle that processing never stops.

Design note, same pattern as segmentation.py: the retry/skip state
machine (`_process_segment` / `_attempt_with_retry`) takes the MT and
TTS functions as injectable dependencies, so it's unit-testable with
fake functions that succeed/fail on command — no real API calls needed
to verify the retry logic itself. See tools/test_translation_queue_logic.py.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

import aiohttp

from .config import Config
from .segmentation import Segment

logger = logging.getLogger("translation_pipeline")


@dataclass(frozen=True)
class TranslatedSegment:
    session_id: str
    sequence: int
    source_transcript: str
    translated_text: Optional[str]
    audio: Optional[bytes]   # MP3 bytes from ElevenLabs, or None if skipped
    state: str                # "delivered" | "skipped"
    error: Optional[str] = None


OnTranslated = Callable[[TranslatedSegment], Union[None, Awaitable[None]]]
TranslateFn = Callable[[str], Awaitable[str]]
SynthesizeFn = Callable[[str], Awaitable[bytes]]


# ---------------------------------------------------------------------
# Provider calls — same approach as the benchmark project's
# mt_providers.py / tts_providers.py, trimmed down to "return the
# result or raise" since this module handles retry/skip itself rather
# than reporting TTFB/timing.
# ---------------------------------------------------------------------

async def translate_text(cfg: Config, text: str, source_lang: str = "ID", target_lang: str = "EN-US") -> str:
    """Defaults preserve the original lecture-direction behavior
    (Indonesian -> English) for existing call sites. Question Mode
    calls this with source_lang="EN", target_lang="ID" for the
    student-to-teacher direction."""
    if not cfg.deepl_api_key:
        raise RuntimeError("DEEPL_API_KEY not set")

    async with aiohttp.ClientSession() as session:
        async with session.post(
            cfg.deepl_api_url,
            headers={"Authorization": f"DeepL-Auth-Key {cfg.deepl_api_key}"},
            data={"text": text, "source_lang": source_lang, "target_lang": target_lang},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"DeepL HTTP {resp.status}: {body[:200]}")
            data = await resp.json()
            return data["translations"][0]["text"]


async def synthesize_speech(cfg: Config, text: str, voice_id: Optional[str] = None) -> bytes:
    """voice_id defaults to cfg.elevenlabs_voice_id (the lecture/English
    voice) if not given explicitly — Question Mode passes
    cfg.elevenlabs_voice_id_question (or the same fallback) instead,
    since the question's output is Indonesian, not English."""
    voice = voice_id or cfg.elevenlabs_voice_id
    if not cfg.elevenlabs_api_key or not voice:
        raise RuntimeError("ELEVENLABS_API_KEY / voice id not set")

    import websockets as ws_lib

    uri = (
        f"wss://api.elevenlabs.io/v1/text-to-speech/{voice}"
        f"/stream-input?model_id=eleven_turbo_v2_5"
    )
    audio_chunks: list[bytes] = []

    async with ws_lib.connect(uri) as ws:
        await ws.send(json.dumps({
            "text": " ",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.8},
            "xi_api_key": cfg.elevenlabs_api_key,
        }))
        await ws.send(json.dumps({"text": text}))
        await ws.send(json.dumps({"text": ""}))

        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=20)
            data = json.loads(raw)
            if data.get("audio"):
                audio_chunks.append(base64.b64decode(data["audio"]))
            if data.get("isFinal"):
                break

    if not audio_chunks:
        raise RuntimeError("No audio chunks received from ElevenLabs")
    return b"".join(audio_chunks)


# ---------------------------------------------------------------------
# Queue + retry/skip state machine
# ---------------------------------------------------------------------

class TranslationQueue:
    """One instance per teacher session."""

    def __init__(
        self,
        cfg: Config,
        session_id: str,
        on_translated: Optional[OnTranslated] = None,
        translate_fn: Optional[TranslateFn] = None,
        synthesize_fn: Optional[SynthesizeFn] = None,
    ) -> None:
        self._cfg = cfg
        self._session_id = session_id
        self._on_translated = on_translated or self._default_debug_sink
        self._translate_fn = translate_fn or (lambda text: translate_text(cfg, text))
        self._synthesize_fn = synthesize_fn or (lambda text: synthesize_speech(cfg, text))

        self._queue: asyncio.Queue[Segment] = asyncio.Queue()
        self._worker_tasks: list[asyncio.Task] = []

        self.segments_delivered = 0
        self.segments_skipped = 0
        self.segments_silent = 0

    def start(self) -> None:
        for _ in range(self._cfg.translation_num_workers):
            self._worker_tasks.append(asyncio.create_task(self._worker_loop()))
        logger.info(
            "Translation queue started for session %s (%d workers)",
            self._session_id, self._cfg.translation_num_workers,
        )

    async def enqueue(self, segment: Segment) -> None:
        """Wired as Segmenter's on_segment callback. Non-blocking — a
        full teacher-audio-length backlog is accepted, per Rule 6."""
        await self._queue.put(segment)

    async def stop(self) -> None:
        # Let already-enqueued segments finish processing rather than
        # discarding them — same "don't silently drop in-flight work"
        # principle as the Jitter Buffer's flush() and Teacher Audio
        # Ingest's drain-on-shutdown.
        try:
            await asyncio.wait_for(self._queue.join(), timeout=self._cfg.translation_drain_timeout_s)
        except asyncio.TimeoutError:
            logger.warning(
                "Session %s: translation queue didn't fully drain within %.0fs, "
                "cancelling remaining work.", self._session_id, self._cfg.translation_drain_timeout_s,
            )

        for task in self._worker_tasks:
            task.cancel()
        for task in self._worker_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

        logger.info(
            "Translation queue stopped for session %s (delivered=%d skipped=%d)",
            self._session_id, self.segments_delivered, self.segments_skipped,
        )

    async def _worker_loop(self) -> None:
        while True:
            segment = await self._queue.get()
            try:
                await self._process_segment(segment)
            finally:
                self._queue.task_done()

    async def _process_segment(self, segment: Segment) -> None:
        if not segment.transcript:
            # A silence/noise segment with nothing recognized. Still
            # emit a result (state="skipped", error=None) rather than
            # emitting nothing at all. Ordered Student Playback needs
            # every sequence number accounted for, deterministically —
            # otherwise its reorder buffer would have to guess or
            # time out waiting for a segment number that was never
            # going to arrive. See app/playback.py.
            self.segments_silent += 1
            await self._emit(TranslatedSegment(
                session_id=segment.session_id,
                sequence=segment.sequence,
                source_transcript="",
                translated_text=None,
                audio=None,
                state="skipped",
                error=None,
            ))
            return

        translated_text, mt_error = await self._attempt_with_retry(
            lambda: self._translate_fn(segment.transcript), stage="MT",
        )
        if mt_error:
            await self._emit(self._skipped_result(segment, stage="MT", error=mt_error))
            return

        audio, tts_error = await self._attempt_with_retry(
            lambda: self._synthesize_fn(translated_text), stage="TTS",
        )
        if tts_error:
            await self._emit(self._skipped_result(
                segment, stage="TTS", error=tts_error, translated_text=translated_text,
            ))
            return

        self.segments_delivered += 1
        await self._emit(TranslatedSegment(
            session_id=segment.session_id,
            sequence=segment.sequence,
            source_transcript=segment.transcript,
            translated_text=translated_text,
            audio=audio,
            state="delivered",
        ))

    async def _attempt_with_retry(self, fn: Callable[[], Awaitable], stage: str):
        last_error: Optional[str] = None
        for attempt in range(1, self._cfg.translation_max_attempts + 1):
            try:
                value = await fn()
                return value, None
            except Exception as e:  # noqa: BLE001 -- any provider failure is handled uniformly here
                last_error = str(e)
                logger.warning(
                    "Session %s: %s attempt %d/%d failed: %s",
                    self._session_id, stage, attempt, self._cfg.translation_max_attempts, last_error,
                )
        return None, last_error

    def _skipped_result(
        self, segment: Segment, stage: str, error: str, translated_text: Optional[str] = None,
    ) -> TranslatedSegment:
        self.segments_skipped += 1
        result = TranslatedSegment(
            session_id=segment.session_id,
            sequence=segment.sequence,
            source_transcript=segment.transcript,
            translated_text=translated_text,  # preserved if MT already succeeded before TTS failed
            audio=None,
            state="skipped",
            error=f"{stage} failed after {self._cfg.translation_max_attempts} attempt(s): {error}",
        )
        logger.info("Session %s segment #%d skipped: %s", segment.session_id, segment.sequence, result.error)
        return result

    async def _emit(self, result: TranslatedSegment) -> None:
        outcome = self._on_translated(result)
        if asyncio.iscoroutine(outcome):
            await outcome

    # ---------------------------------------------------------------
    # Default debug sink — writes translated audio + text to disk so
    # this is testable before Student Playback exists.
    # ---------------------------------------------------------------

    def _default_debug_sink(self, result: TranslatedSegment) -> None:
        write_debug_output(self._cfg, result)


def write_debug_output(cfg: Config, result: TranslatedSegment) -> None:
    """Writes a translated segment's text + audio to disk for manual
    inspection. Standalone (not just a TranslationQueue method) so it
    can be composed alongside the real Student Playback wiring too —
    see session.py, which calls this *and* forwards the result to
    StudentPlaybackController, rather than one replacing the other."""
    out_dir = Path(cfg.debug_output_dir) / "translated"
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"{result.session_id}_seg{result.sequence:03d}_{result.state}"

    lines = [
        f"source: {result.source_transcript!r}",
        f"translated: {result.translated_text!r}",
        f"state: {result.state}",
    ]
    if result.error:
        lines.append(f"error: {result.error}")
    base.with_suffix(".txt").write_text("\n".join(lines), encoding="utf-8")

    if result.audio:
        # ElevenLabs' streaming endpoint returns MP3 by default.
        base.with_suffix(".mp3").write_bytes(result.audio)
