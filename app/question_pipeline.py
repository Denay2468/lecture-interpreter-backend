"""Question Pipeline — Student -> Teacher direction (Question Mode).

English student question -> STT (English) -> MT (English -> Indonesian)
-> TTS (Indonesian) -> delivered to the teacher.

This is the reverse of the lecture pipeline, and deliberately simpler
in one respect: by the time this runs, the question's audio is already
a complete, bounded clip (captured between ask_press and ask_release),
not a continuous stream needing live segmentation. So STT here is a
one-shot call — send everything, get one transcript back — rather than
Segmentation's long-lived streaming connection with boundary detection.

STT is normally streamed WHILE the student speaks (QuestionTranscriber below):
the Deepgram connection opens at ask_press, so by release only the trailing
words are left. transcribe_question_audio is kept as the fallback if that
streaming path fails.

Implements Rule 8: one retry per stage, then a clear, visible failure
reported back to the *student* (not silently dropped the way a failed
lecture segment is per Rule 7 — a failed question is total loss of that
whole interaction, and the student needs to know to try again).
"""
from __future__ import annotations

import asyncio
import time
import logging
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .translation_pipeline import synthesize_speech, translate_text

logger = logging.getLogger("question_pipeline")


@dataclass(frozen=True)
class QuestionResult:
    success: bool
    source_transcript: Optional[str] = None
    translated_text: Optional[str] = None
    audio: Optional[bytes] = None
    error: Optional[str] = None


async def transcribe_question_audio(cfg: Config, audio: bytes) -> str:
    """One-shot Deepgram transcription of the student's complete
    question clip, in English. Opens a connection, sends everything,
    closes — no long-lived segmentation boundary logic needed here,
    unlike app/segmentation.py's continuous streaming connection."""
    if not cfg.deepgram_api_key:
        raise RuntimeError("DEEPGRAM_API_KEY not set")

    from deepgram import AsyncDeepgramClient
    from deepgram.core.events import EventType

    # UtteranceEnd is a silence-detection signal — it needs actual quiet
    # audio to fire, not just "the stream was closed." A question clip
    # captured between ask_press/ask_release has no guarantee of
    # trailing silence (the student might stop talking right as they
    # release the button), so without this padding, Deepgram has
    # nothing to detect and we'd fall back to the 10s timeout on every
    # question — which is exactly what happened on the first real test
    # of this pipeline (see the fix commit this comment shipped with).
    # Padding past utterance_end_ms guarantees a real gap for it to see.
    silence_padding_ms = cfg.segmentation_utterance_end_ms + 300
    silence_bytes = bytes(int(cfg.sample_rate_hz * cfg.bytes_per_sample * (silence_padding_ms / 1000)))
    audio = audio + silence_bytes

    client = AsyncDeepgramClient(api_key=cfg.deepgram_api_key)
    transcript_parts: list[str] = []
    done = asyncio.Event()

    async def on_message(message) -> None:
        msg_type = getattr(message, "type", None)
        if msg_type == "Results" and bool(getattr(message, "is_final", False)):
            try:
                text = message.channel.alternatives[0].transcript
            except (AttributeError, IndexError):
                text = ""
            if text:
                transcript_parts.append(text)
        elif msg_type == "UtteranceEnd":
            done.set()

    async with client.listen.v1.connect(
        model=cfg.deepgram_model,
        language="en",  # student speaks English — see architecture spec's language model
        smart_format=True,
        interim_results=True,
        endpointing=cfg.segmentation_endpointing_ms,
        utterance_end_ms=str(cfg.segmentation_utterance_end_ms),
        vad_events=True,
        encoding="linear16",
        sample_rate=cfg.sample_rate_hz,
        channels=cfg.channels,
    ) as connection:
        connection.on(EventType.MESSAGE, on_message)
        listen_task = asyncio.create_task(connection.start_listening())

        # Sent as fast as the connection allows, NOT paced in real time.
        # An earlier version paced these sends (sleep 0.1s per 100ms
        # chunk) on the theory that blasting the clip let
        # send_close_stream() beat recognition of the last words. That
        # theory was wrong: the "truncated" transcript was the speaker
        # trailing off, and the same text came back with and without
        # pacing. Pacing only made each question land roughly 2-2.5s
        # later in the logs, so it was removed. The trailing-silence
        # padding above is the fix that mattered: it ended the 10s
        # UtteranceEnd timeout.
        chunk_size = max(1, int(cfg.sample_rate_hz * cfg.bytes_per_sample * 0.1))  # ~100ms chunks
        for i in range(0, len(audio), chunk_size):
            await connection.send_media(audio[i:i + chunk_size])
        await connection.send_close_stream()

        try:
            await asyncio.wait_for(done.wait(), timeout=10)
        except asyncio.TimeoutError:
            # Fall through with whatever was transcribed so far rather
            # than treating a missing UtteranceEnd as total failure —
            # the question audio is short and bounded, so partial
            # transcript is still likely usable.
            logger.warning("Question transcription timed out waiting for UtteranceEnd; using partial result")

        listen_task.cancel()
        try:
            await listen_task
        except asyncio.CancelledError:
            pass

    return " ".join(transcript_parts).strip()


class DeepgramQuestionStream:
    """The Deepgram-specific half of streaming question STT.

    The same SDK calls as transcribe_question_audio above (and
    Segmenter.start in segmentation.py), split into open / send /
    close_stream / close so the orchestration in QuestionTranscriber can be
    unit tested with a fake that needs no network. Events go to
    on_event(kind, text), where kind is one of: "final" (a finalized chunk
    of text), "utterance_end", "error", or "closed" (the connection ended).
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._ctx = None
        self._conn = None
        self._listen_task: Optional[asyncio.Task] = None

    async def open(self, on_event) -> None:
        cfg = self._cfg
        if not cfg.deepgram_api_key:
            raise RuntimeError("DEEPGRAM_API_KEY not set")

        from deepgram import AsyncDeepgramClient
        from deepgram.core.events import EventType

        async def on_message(message) -> None:
            msg_type = getattr(message, "type", None)
            if msg_type == "Results" and bool(getattr(message, "is_final", False)):
                try:
                    text = message.channel.alternatives[0].transcript
                except (AttributeError, IndexError):
                    text = ""
                on_event("final", text)
            elif msg_type == "UtteranceEnd":
                on_event("utterance_end", "")
            elif msg_type == "Error":
                on_event("error", str(message))

        client = AsyncDeepgramClient(api_key=cfg.deepgram_api_key)
        self._ctx = client.listen.v1.connect(
            model=cfg.deepgram_model,
            language="en",  # student speaks English - see architecture spec's language model
            smart_format=True,
            interim_results=True,
            endpointing=cfg.segmentation_endpointing_ms,
            utterance_end_ms=str(cfg.segmentation_utterance_end_ms),
            vad_events=True,
            encoding="linear16",
            sample_rate=cfg.sample_rate_hz,
            channels=cfg.channels,
        )
        self._conn = await self._ctx.__aenter__()
        self._conn.on(EventType.MESSAGE, on_message)
        self._listen_task = asyncio.create_task(self._conn.start_listening())
        # When Deepgram closes the connection after CloseStream, every final
        # result has been delivered, which is the most reliable "done" signal.
        self._listen_task.add_done_callback(lambda _task: on_event("closed", ""))

    async def send(self, audio: bytes) -> None:
        await self._conn.send_media(audio)

    async def close_stream(self) -> None:
        await self._conn.send_close_stream()

    async def close(self) -> None:
        if self._listen_task is not None:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        if self._ctx is not None and self._conn is not None:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


class QuestionTranscriber:
    """Streams one question to speech-to-text WHILE the student is still
    speaking, instead of after they release Ask.

    Why: opening a Deepgram connection took roughly 3-5s in testing, and the
    old flow did that after release, so STT was 5.1-6.2s of a 7.7-9.7s
    question. Now the connection opens at press and the audio flows live; at
    release only the trailing silence and the final words are left.

    Lifecycle (driven by SessionCoordinator):
      start()   - on press; connects in the background
      feed()    - per audio frame; safe before the connection is up, frames
                  queue and are sent in order once it is
      finish()  - on release; sends trailing silence, waits for the end of
                  speech, returns the transcript. Raises on any streaming
                  failure so the caller can fall back to the one-shot call.
      abort()   - discard (accidental press, student disconnected)
    """

    def __init__(self, cfg: Config, session_id: str, stream=None,
                 utterance_end_timeout_s: float = 10.0, connect_timeout_s: float = 15.0) -> None:
        self._cfg = cfg
        self._session_id = session_id
        self._stream = stream if stream is not None else DeepgramQuestionStream(cfg)
        self._utterance_end_timeout_s = utterance_end_timeout_s
        self._connect_timeout_s = connect_timeout_s

        self._queue: asyncio.Queue = asyncio.Queue()  # (chunk, is_padding) or None = end of audio
        self._parts: list[str] = []
        self._done = asyncio.Event()
        self._error: Optional[str] = None
        self._pump_task: Optional[asyncio.Task] = None
        self._finishing = False
        self._closed = False
        self._padding_sent = False
        self._bytes_fed = 0
        self._t_start = time.monotonic()
        self.connect_s: Optional[float] = None

    def start(self) -> None:
        self._t_start = time.monotonic()
        self._pump_task = asyncio.create_task(self._pump())

    def feed(self, payload: bytes) -> None:
        if self._closed or self._finishing:
            return
        self._bytes_fed += len(payload)
        self._queue.put_nowait((payload, False))

    async def finish(self) -> str:
        if self._closed or self._finishing:
            raise RuntimeError("question transcriber already finished or aborted")
        self._finishing = True
        t_release = time.monotonic()

        # UtteranceEnd needs real quiet audio to fire (see
        # transcribe_question_audio). Padding past utterance_end_ms
        # guarantees a gap for it to see.
        step = max(1, int(self._cfg.sample_rate_hz * self._cfg.bytes_per_sample * 0.1))
        padding_ms = self._cfg.segmentation_utterance_end_ms + 300
        silence = bytes(int(self._cfg.sample_rate_hz * self._cfg.bytes_per_sample * (padding_ms / 1000)))
        for i in range(0, len(silence), step):
            self._queue.put_nowait((silence[i:i + step], True))
        self._queue.put_nowait(None)

        try:
            try:
                await asyncio.wait_for(self._pump_task, timeout=self._connect_timeout_s)
            except asyncio.TimeoutError:
                raise RuntimeError("streaming STT did not finish connecting and sending in time")
            if self._error:
                raise RuntimeError(self._error)

            try:
                await asyncio.wait_for(self._done.wait(), timeout=self._utterance_end_timeout_s)
            except asyncio.TimeoutError:
                logger.warning("Session %s: question STT timed out waiting for end of speech; using partial result",
                               self._session_id)
            if self._error:
                raise RuntimeError(self._error)

            logger.info(
                "Session %s question STT streamed: connected %.1fs after press, %d bytes fed live, "
                "%.1fs from release to transcript",
                self._session_id, self.connect_s if self.connect_s is not None else -1.0,
                self._bytes_fed, time.monotonic() - t_release,
            )
            return " ".join(self._parts).strip()
        finally:
            await self._teardown()

    async def abort(self) -> None:
        if self._closed:
            return
        await self._teardown()

    async def _teardown(self) -> None:
        self._closed = True
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
        try:
            await self._stream.close()
        except Exception:  # noqa: BLE001 - best-effort teardown
            logger.debug("Closing question STT stream failed", exc_info=True)

    async def _pump(self) -> None:
        try:
            await self._stream.open(self._on_event)
            self.connect_s = time.monotonic() - self._t_start
            while True:
                item = await self._queue.get()
                if item is None:
                    break
                chunk, is_padding = item
                await self._stream.send(chunk)
                if is_padding:
                    self._padding_sent = True
            await self._stream.close_stream()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - reported through finish()
            self._error = f"{type(e).__name__}: {e}"
            self._done.set()

    def _on_event(self, kind: str, text: str) -> None:
        if kind == "final":
            if text:
                self._parts.append(text)
        elif kind == "utterance_end":
            # Only the one our trailing padding causes means "done". The
            # student may pause mid-question, which fires one earlier. Known
            # small risk: a late event from an earlier pause landing after
            # the padding started would end the wait a little early.
            if self._padding_sent:
                self._done.set()
        elif kind == "error":
            self._error = text or "unknown Deepgram error"
            self._done.set()
        elif kind == "closed":
            if not self._padding_sent and not self._closed:
                self._error = self._error or "Deepgram connection closed before the question ended"
            self._done.set()


async def _attempt_with_retry(fn, stage: str, session_id: str, max_attempts: int = 2):
    last_error: Optional[str] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn(), None
        except Exception as e:  # noqa: BLE001 -- uniform handling across STT/MT/TTS failures
            last_error = str(e)
            logger.warning(
                "Session %s question %s attempt %d/%d failed: %s",
                session_id, stage, attempt, max_attempts, last_error,
            )
    return None, last_error


async def process_question(
    cfg: Config,
    session_id: str,
    audio: bytes,
    transcribe_fn=None,
    translate_fn=None,
    synthesize_fn=None,
    transcriber=None,
) -> QuestionResult:
    """Runs the full STT -> MT -> TTS chain for one captured question
    clip. Returns a QuestionResult either way — never raises — so the
    caller (SessionCoordinator) can always deliver *something* back
    (either the translated audio to the teacher, or a failure notice
    to the student).

    transcribe_fn/translate_fn/synthesize_fn are injectable (default to
    the real Deepgram/DeepL/ElevenLabs calls above) so the retry/skip
    logic itself can be unit tested with fake functions — same pattern
    as TranslationQueue in translation_pipeline.py. See
    tools/test_question_pipeline_logic.py.
    """
    transcribe_fn = transcribe_fn or (lambda a: transcribe_question_audio(cfg, a))
    batch_transcribe_fn = transcribe_fn

    async def stt(a: bytes) -> str:
        # Streaming path: the audio has been going to Deepgram since the
        # student pressed Ask, so this just collects the result. If that
        # failed for any reason, fall back to the one-shot call on the audio
        # session.py buffered in parallel. Streaming can only make a
        # question faster; it can't make it fail.
        if transcriber is not None:
            try:
                return await transcriber.finish()
            except Exception as e:  # noqa: BLE001
                logger.warning("Session %s: streaming question STT failed (%s); falling back to one-shot STT",
                               session_id, e)
        return await batch_transcribe_fn(a)
    translate_fn = translate_fn or (lambda t: translate_text(cfg, t, source_lang="EN", target_lang="ID"))
    voice_id = cfg.elevenlabs_voice_id_question or cfg.elevenlabs_voice_id
    synthesize_fn = synthesize_fn or (lambda t: synthesize_speech(cfg, t, voice_id=voice_id))

    t_start = time.monotonic()
    transcript, stt_error = await _attempt_with_retry(
        lambda: stt(audio), "STT", session_id, cfg.translation_max_attempts,
    )
    if stt_error or not transcript:
        return QuestionResult(success=False, error=f"Speech recognition failed: {stt_error or 'no speech detected'}")

    t_stt_done = time.monotonic()
    translated, mt_error = await _attempt_with_retry(
        lambda: translate_fn(transcript), "MT", session_id, cfg.translation_max_attempts,
    )
    if mt_error:
        return QuestionResult(success=False, source_transcript=transcript, error=f"Translation failed: {mt_error}")

    t_mt_done = time.monotonic()
    audio_out, tts_error = await _attempt_with_retry(
        lambda: synthesize_fn(translated), "TTS", session_id, cfg.translation_max_attempts,
    )
    if tts_error:
        return QuestionResult(
            success=False, source_transcript=transcript, translated_text=translated,
            error=f"Speech synthesis failed: {tts_error}",
        )

    t_done = time.monotonic()
    logger.info(
        "Session %s question pipeline timings: STT %.1fs, MT %.1fs, TTS %.1fs, total %.1fs",
        session_id, t_stt_done - t_start, t_mt_done - t_stt_done, t_done - t_mt_done, t_done - t_start,
    )
    return QuestionResult(success=True, source_transcript=transcript, translated_text=translated, audio=audio_out)
