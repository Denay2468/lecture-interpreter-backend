#!/usr/bin/env python3
"""
Local test of streaming question STT: QuestionTranscriber's orchestration
and the streaming-with-fallback behaviour of process_question. A fake
stream stands in for Deepgram, so there is no network and no API key.

What this can't cover: the real Deepgram adapter (DeepgramQuestionStream).
That mirrors SDK calls already used by transcribe_question_audio and the
lecture Segmenter, but only a live run exercises it.

Usage:
    python tools/test_question_streaming_logic.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.question_pipeline import QuestionTranscriber, process_question  # noqa: E402

FAILURES = 0
FINALS = ("Can you explain", "more about that")
FULL_TEXT = "Can you explain more about that"


def check(condition: bool, description: str) -> None:
    global FAILURES
    print(f"  [{'PASS' if condition else 'FAIL'}] {description}")
    if not condition:
        FAILURES += 1


class FakeStream:
    """Stands in for DeepgramQuestionStream. Delivers its final text a moment
    AFTER close_stream, like the real service, so an implementation that
    stops waiting too early returns a short transcript and fails the test."""

    def __init__(self, open_delay=0.0, fail_open=False, early_utterance_end=False,
                 close_early=False, never_finish=False, end_with="utterance_end"):
        self.open_delay = open_delay
        self.fail_open = fail_open
        self.early_utterance_end = early_utterance_end
        self.close_early = close_early
        self.never_finish = never_finish
        self.end_with = end_with
        self.sent: list[bytes] = []
        self.stream_closed = False
        self.closed = False
        self._on_event = None

    async def open(self, on_event) -> None:
        await asyncio.sleep(self.open_delay)
        if self.fail_open:
            raise RuntimeError("connect failed")
        self._on_event = on_event

    async def send(self, audio: bytes) -> None:
        self.sent.append(audio)
        n = len(self.sent)
        if self.never_finish and n == 1:
            self._on_event("final", "partial words")
        if self.early_utterance_end and n == 2:
            self._on_event("utterance_end", "")  # the student paused mid-question
        if self.close_early and n == 1:
            self._on_event("closed", "")

    async def close_stream(self) -> None:
        self.stream_closed = True
        if self.never_finish:
            return

        def emit() -> None:
            for text in FINALS:
                self._on_event("final", text)
            self._on_event(self.end_with, "")

        asyncio.get_running_loop().call_later(0.05, emit)

    async def close(self) -> None:
        self.closed = True


def make(fake: FakeStream, **kwargs) -> QuestionTranscriber:
    return QuestionTranscriber(Config(), "test", stream=fake, **kwargs)


async def raises_runtime_error(coro) -> str | None:
    try:
        await coro
    except RuntimeError as e:
        return str(e)
    return None


async def test_buffers_before_connect_then_pads() -> None:
    print("\ntest_frames_before_connection_are_buffered_in_order_then_padded")
    fake = FakeStream(open_delay=0.1)
    t = make(fake)
    t.start()
    chunks = [bytes([i]) * 640 for i in (1, 2, 3)]
    for c in chunks:
        t.feed(c)  # the connection doesn't exist yet
    text = await t.finish()
    padding = fake.sent[3:]
    check(fake.sent[:3] == chunks, "audio fed before the connection was up arrives complete and in order")
    check(padding and all(set(p) == {0} for p in padding), "everything after the real audio is silence")
    check(sum(len(p) for p in padding) == 41600, f"1.3s of padding (41600 bytes), got {sum(len(p) for p in padding)}")
    check(text == FULL_TEXT, f"transcript assembled from final chunks (got {text!r})")
    check(fake.stream_closed and fake.closed, "stream closed and torn down after finish")


async def test_mid_question_pause_is_ignored() -> None:
    print("\ntest_mid_question_pause_does_not_end_the_wait_early")
    fake = FakeStream(early_utterance_end=True)
    t = make(fake)
    t.start()
    for i in (1, 2, 3):
        t.feed(bytes([i]) * 640)
    text = await t.finish()
    check(text == FULL_TEXT, f"waited for the real end of speech, so the last words are kept (got {text!r})")


async def test_closed_connection_is_an_end_signal() -> None:
    print("\ntest_connection_closing_after_padding_ends_the_wait")
    fake = FakeStream(end_with="closed")
    t = make(fake)
    t.start()
    t.feed(b"\x01" * 640)
    text = await t.finish()
    check(text == FULL_TEXT, f"connection close counts as done once the padding was sent (got {text!r})")


async def test_early_close_raises() -> None:
    print("\ntest_connection_closing_before_the_question_ends_raises")
    fake = FakeStream(close_early=True)
    t = make(fake)
    t.start()
    t.feed(b"\x01" * 640)
    err = await raises_runtime_error(t.finish())
    check(err is not None and "closed" in err, f"raises so the caller can fall back (got {err!r})")


async def test_timeout_returns_partial() -> None:
    print("\ntest_timeout_returns_partial_transcript")
    fake = FakeStream(never_finish=True)
    t = make(fake, utterance_end_timeout_s=0.2)
    t.start()
    t.feed(b"\x01" * 640)
    text = await t.finish()
    check(text == "partial words", f"returns what was finalized instead of hanging (got {text!r})")


async def test_open_failure_raises() -> None:
    print("\ntest_open_failure_raises_so_caller_can_fall_back")
    fake = FakeStream(fail_open=True)
    t = make(fake)
    t.start()
    t.feed(b"\x01" * 640)
    err = await raises_runtime_error(t.finish())
    check(err is not None and "connect failed" in err, f"error surfaces from finish() (got {err!r})")


async def test_abort_during_connect() -> None:
    print("\ntest_abort_during_connect_closes_and_blocks_finish")
    fake = FakeStream(open_delay=0.5)
    t = make(fake)
    t.start()
    t.feed(b"\x01" * 640)
    await t.abort()
    check(fake.closed, "stream closed")
    check(fake.sent == [], "nothing was ever sent")
    err = await raises_runtime_error(t.finish())
    check(err is not None, "finish() after abort raises instead of hanging")


class FakeLive:
    def __init__(self, text=None, error=None):
        self.text, self.error = text, error

    async def finish(self) -> str:
        if self.error:
            raise RuntimeError(self.error)
        return self.text


async def fake_translate(text: str) -> str:
    return "id:" + text


async def fake_tts(text: str) -> bytes:
    return b"audio"


async def test_pipeline_uses_streamed_transcript() -> None:
    print("\ntest_pipeline_uses_streamed_transcript_without_one_shot_stt")
    batch_calls: list = []

    async def batch(audio: bytes) -> str:
        batch_calls.append(audio)
        return "batch transcript"

    r = await process_question(Config(), "s", b"buffered", transcribe_fn=batch, translate_fn=fake_translate,
                               synthesize_fn=fake_tts, transcriber=FakeLive(text="live transcript"))
    check(r.success and r.source_transcript == "live transcript", "result carries the streamed transcript")
    check(batch_calls == [], "one-shot STT never called")


async def test_pipeline_falls_back() -> None:
    print("\ntest_pipeline_falls_back_to_one_shot_stt_when_streaming_fails")
    batch_calls: list = []

    async def batch(audio: bytes) -> str:
        batch_calls.append(audio)
        return "batch transcript"

    r = await process_question(Config(), "s", b"buffered", transcribe_fn=batch, translate_fn=fake_translate,
                               synthesize_fn=fake_tts, transcriber=FakeLive(error="boom"))
    check(r.success and r.source_transcript == "batch transcript", "question still succeeds")
    check(batch_calls == [b"buffered"], "one-shot STT ran on the audio buffered in parallel")


async def test_pipeline_fails_visibly() -> None:
    print("\ntest_pipeline_fails_visibly_when_both_paths_fail")

    async def batch(audio: bytes) -> str:
        raise RuntimeError("batch down")

    r = await process_question(Config(), "s", b"buffered", transcribe_fn=batch, translate_fn=fake_translate,
                               synthesize_fn=fake_tts, transcriber=FakeLive(error="boom"))
    check(not r.success and r.error, f"a clear failure result, not a hang or crash (error: {r.error!r})")


async def main() -> int:
    await test_buffers_before_connect_then_pads()
    await test_mid_question_pause_is_ignored()
    await test_closed_connection_is_an_end_signal()
    await test_early_close_raises()
    await test_timeout_returns_partial()
    await test_open_failure_raises()
    await test_abort_during_connect()
    await test_pipeline_uses_streamed_transcript()
    await test_pipeline_falls_back()
    await test_pipeline_fails_visibly()
    print()
    if FAILURES:
        print(f"{FAILURES} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
