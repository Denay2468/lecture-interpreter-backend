#!/usr/bin/env python3
"""
Local test of the Translation Queue's retry/skip state machine — no
DeepL or ElevenLabs calls, no network, no API keys required.

Uses fake translate_fn/synthesize_fn (dependency injection, same
pattern as tools/test_segmenter_logic.py) to verify: successful
delivery, retry-then-succeed, retry-then-skip for both MT and TTS
failures, silence segments being skipped quietly, FIFO ordering, and
that stop() drains already-enqueued work rather than dropping it.

Usage:
    python tools/test_translation_queue_logic.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.segmentation import Segment  # noqa: E402
from app.translation_pipeline import TranslationQueue  # noqa: E402

FAILURES = 0


def check(condition: bool, description: str) -> None:
    global FAILURES
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {description}")
    if not condition:
        FAILURES += 1


def make_segment(seq: int, transcript: str = "halo dunia") -> Segment:
    return Segment(
        session_id="test-session", sequence=seq, audio=b"\x00\x01" * 100,
        transcript=transcript, trigger="speech_final", duration_ms=1000.0,
    )


async def test_successful_delivery() -> None:
    print("\ntest_successful_delivery")
    results = []

    async def fake_translate(text: str) -> str:
        return f"[EN] {text}"

    async def fake_synthesize(text: str) -> bytes:
        return b"fake-mp3-bytes"

    cfg = Config(translation_num_workers=1)
    q = TranslationQueue(cfg, "test-session", on_translated=lambda r: results.append(r),
                          translate_fn=fake_translate, synthesize_fn=fake_synthesize)
    q.start()
    await q.enqueue(make_segment(0, "halo dunia"))
    await q.stop()

    check(len(results) == 1, "one result produced")
    check(results[0].state == "delivered", f"state is 'delivered' (got {results[0].state!r})")
    check(results[0].translated_text == "[EN] halo dunia", "translated text is correct")
    check(results[0].audio == b"fake-mp3-bytes", "audio bytes came through")
    check(q.segments_delivered == 1 and q.segments_skipped == 0, "counters correct")


async def test_mt_retry_then_succeed() -> None:
    print("\ntest_mt_retry_then_succeed")
    results = []
    call_count = {"n": 0}

    async def flaky_translate(text: str) -> str:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated transient DeepL failure")
        return "translated on retry"

    async def fake_synthesize(text: str) -> bytes:
        return b"audio"

    cfg = Config(translation_num_workers=1)
    q = TranslationQueue(cfg, "test-session", on_translated=lambda r: results.append(r),
                          translate_fn=flaky_translate, synthesize_fn=fake_synthesize)
    q.start()
    await q.enqueue(make_segment(0))
    await q.stop()

    check(call_count["n"] == 2, "translate was called twice (first failed, retry succeeded)")
    check(len(results) == 1 and results[0].state == "delivered", "segment delivered after retry succeeded")


async def test_mt_fails_twice_is_skipped() -> None:
    print("\ntest_mt_fails_twice_is_skipped")
    results = []

    async def always_fails(text: str) -> str:
        raise RuntimeError("DeepL is down")

    async def fake_synthesize(text: str) -> bytes:
        return b"audio"

    cfg = Config(translation_num_workers=1)
    q = TranslationQueue(cfg, "test-session", on_translated=lambda r: results.append(r),
                          translate_fn=always_fails, synthesize_fn=fake_synthesize)
    q.start()
    await q.enqueue(make_segment(0))
    await q.stop()

    check(len(results) == 1, "one result still produced (skip, not silence)")
    check(results[0].state == "skipped", f"state is 'skipped' (got {results[0].state!r})")
    check(results[0].error is not None and "MT" in results[0].error, f"error mentions MT stage (got {results[0].error!r})")
    check(results[0].audio is None, "no audio for a skipped segment")
    check(q.segments_skipped == 1 and q.segments_delivered == 0, "counters correct")


async def test_tts_fails_twice_is_skipped() -> None:
    print("\ntest_tts_fails_twice_is_skipped")
    results = []

    async def fake_translate(text: str) -> str:
        return "translated fine"

    async def always_fails(text: str) -> bytes:
        raise RuntimeError("ElevenLabs is down")

    cfg = Config(translation_num_workers=1)
    q = TranslationQueue(cfg, "test-session", on_translated=lambda r: results.append(r),
                          translate_fn=fake_translate, synthesize_fn=always_fails)
    q.start()
    await q.enqueue(make_segment(0))
    await q.stop()

    check(len(results) == 1, "one result produced")
    check(results[0].state == "skipped", "state is 'skipped'")
    check("TTS" in (results[0].error or ""), f"error mentions TTS stage (got {results[0].error!r})")
    check(results[0].translated_text == "translated fine", "MT succeeded before TTS failed — translated text is still recorded")


async def test_silence_segment_still_emits_a_result() -> None:
    print("\ntest_silence_segment_still_emits_a_result")
    # Updated expectation: silence segments used to produce no result at
    # all, but that made downstream Ordered Playback's reorder buffer
    # unable to tell "still waiting" apart from "this sequence number
    # will never arrive." Now every segment always produces exactly one
    # result, so playback ordering can be fully deterministic.
    results = []
    mt_calls = {"n": 0}

    async def counting_translate(text: str) -> str:
        mt_calls["n"] += 1
        return "should not be called"

    cfg = Config(translation_num_workers=1)
    q = TranslationQueue(cfg, "test-session", on_translated=lambda r: results.append(r),
                          translate_fn=counting_translate, synthesize_fn=lambda t: (_ for _ in ()).throw(AssertionError("should not be called")))
    q.start()
    await q.enqueue(make_segment(0, transcript=""))  # empty transcript = silence/noise segment
    await q.stop()

    check(mt_calls["n"] == 0, "MT is never even attempted for an empty-transcript segment")
    check(len(results) == 1, "exactly one result is still emitted (not zero)")
    check(results[0].state == "skipped" and results[0].error is None, "state is 'skipped' with no error — this is not a failure")
    check(q.segments_silent == 1, "counted separately via segments_silent, not segments_skipped")
    check(q.segments_delivered == 0 and q.segments_skipped == 0, "delivered/skipped (failure) counters unaffected")


async def test_fifo_ordering_with_single_worker() -> None:
    print("\ntest_fifo_ordering_with_single_worker")
    results = []

    async def fake_translate(text: str) -> str:
        return text.upper()

    async def fake_synthesize(text: str) -> bytes:
        return b"audio"

    cfg = Config(translation_num_workers=1)  # single worker => strict FIFO
    q = TranslationQueue(cfg, "test-session", on_translated=lambda r: results.append(r),
                          translate_fn=fake_translate, synthesize_fn=fake_synthesize)
    q.start()
    for i in range(5):
        await q.enqueue(make_segment(i, transcript=f"segment {i}"))
    await q.stop()

    check(len(results) == 5, "all 5 segments produced a result")
    check([r.sequence for r in results] == [0, 1, 2, 3, 4], "results emitted in strict FIFO order with a single worker")


async def test_stop_drains_pending_queue() -> None:
    print("\ntest_stop_drains_pending_queue")
    results = []

    async def slow_translate(text: str) -> str:
        await asyncio.sleep(0.05)
        return text.upper()

    async def fake_synthesize(text: str) -> bytes:
        return b"audio"

    cfg = Config(translation_num_workers=2)
    q = TranslationQueue(cfg, "test-session", on_translated=lambda r: results.append(r),
                          translate_fn=slow_translate, synthesize_fn=fake_synthesize)
    q.start()
    for i in range(6):
        await q.enqueue(make_segment(i))
    # Don't wait — call stop() immediately, while work is still in flight.
    await q.stop()

    check(len(results) == 6, f"stop() drained all 6 enqueued segments rather than dropping in-flight work (got {len(results)})")


async def main() -> int:
    await test_successful_delivery()
    await test_mt_retry_then_succeed()
    await test_mt_fails_twice_is_skipped()
    await test_tts_fails_twice_is_skipped()
    await test_silence_segment_still_emits_a_result()
    await test_fifo_ordering_with_single_worker()
    await test_stop_drains_pending_queue()

    print()
    if FAILURES:
        print(f"{FAILURES} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
