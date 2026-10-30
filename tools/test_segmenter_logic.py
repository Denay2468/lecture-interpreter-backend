#!/usr/bin/env python3
"""
Local test of the Segmentation boundary-triggering logic — no Deepgram
connection, no network, no API key required.

This exercises exactly the part of app/segmentation.py that decides
*when* a segment ends (_on_stt_event / _close_segment / the max-duration
safety net), by feeding it fake frames and fake STT events directly,
bypassing the real Deepgram transport entirely (which is why this can
run anywhere, with no credentials).

What it does NOT test: whether Deepgram's actual `speech_final` and
`UtteranceEnd` signals behave the way this module assumes on real audio.
That part can only be verified by an actual run against the live API —
this test just proves the module's own logic is internally correct
before you spend API credits finding that out.

Usage:
    python tools/test_segmenter_logic.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.protocol import AudioFrame  # noqa: E402
from app.segmentation import Segmenter  # noqa: E402

FAILURES = 0


def check(condition: bool, description: str) -> None:
    global FAILURES
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {description}")
    if not condition:
        FAILURES += 1


def make_frame(seq: int, n_bytes: int = 640) -> AudioFrame:
    # 640 bytes = 20ms of 16kHz mono 16-bit PCM, matching real frame size.
    return AudioFrame(sequence=seq, capture_timestamp_ms=seq * 20, payload=b"\x00\x01" * (n_bytes // 2))


async def test_speech_final_closes_segment() -> None:
    print("\ntest_speech_final_closes_segment")
    segments = []
    cfg = Config(deepgram_api_key=None)  # not used — we never call start()
    seg = Segmenter(cfg, "session-a", on_segment=lambda s: segments.append(s))

    for i in range(5):
        await seg.handle_frame(make_frame(i))
    await seg._on_stt_event(event="results", transcript="halo", speech_final=False)
    check(len(segments) == 0, "no segment closed yet (speech_final was False)")

    for i in range(5, 8):
        await seg.handle_frame(make_frame(i))
    await seg._on_stt_event(event="results", transcript="dunia", speech_final=True)

    check(len(segments) == 1, "exactly one segment closed after speech_final=True")
    s = segments[0]
    check(s.trigger == "speech_final", f"trigger is 'speech_final' (got {s.trigger!r})")
    check(s.transcript == "halo dunia", f"transcript accumulated correctly (got {s.transcript!r})")
    check(len(s.audio) == 8 * 640, f"audio contains all 8 frames' bytes (got {len(s.audio)})")
    check(s.sequence == 0, "first segment has sequence 0")


async def test_utterance_end_is_fallback() -> None:
    print("\ntest_utterance_end_is_fallback")
    segments = []
    cfg = Config(deepgram_api_key=None)
    seg = Segmenter(cfg, "session-b", on_segment=lambda s: segments.append(s))

    for i in range(3):
        await seg.handle_frame(make_frame(i))
    # No speech_final ever fires — only the UtteranceEnd backstop.
    await seg._on_stt_event(event="utterance_end", transcript="", speech_final=False)

    check(len(segments) == 1, "UtteranceEnd alone closes the segment (fallback trigger works)")
    check(segments[0].trigger == "utterance_end", "trigger recorded as 'utterance_end'")


async def test_utterance_end_with_no_audio_is_noop() -> None:
    print("\ntest_utterance_end_with_no_audio_is_noop")
    segments = []
    cfg = Config(deepgram_api_key=None)
    seg = Segmenter(cfg, "session-c", on_segment=lambda s: segments.append(s))

    # An UtteranceEnd with nothing buffered (e.g. right at session start)
    # must not produce an empty/bogus segment.
    await seg._on_stt_event(event="utterance_end", transcript="", speech_final=False)
    check(len(segments) == 0, "UtteranceEnd with no buffered audio produces no segment")


async def test_max_duration_safety_net() -> None:
    print("\ntest_max_duration_safety_net")
    segments = []
    # Tiny cap so the test runs fast, instead of waiting the real 15s default.
    cfg = Config(deepgram_api_key=None, segmentation_max_duration_ms=50)
    seg = Segmenter(cfg, "session-d", on_segment=lambda s: segments.append(s))

    # Feed frames with real small delays so monotonic time actually advances
    # past the 50ms cap, without a speech_final or UtteranceEnd ever firing.
    for i in range(20):
        await seg.handle_frame(make_frame(i))
        await asyncio.sleep(0.01)

    check(len(segments) >= 1, "segment force-closed by max duration cap even with no STT trigger")
    if segments:
        check(segments[0].trigger == "max_duration", f"trigger is 'max_duration' (got {segments[0].trigger!r})")


async def test_multiple_segments_increment_sequence() -> None:
    print("\ntest_multiple_segments_increment_sequence")
    segments = []
    cfg = Config(deepgram_api_key=None)
    seg = Segmenter(cfg, "session-e", on_segment=lambda s: segments.append(s))

    for _ in range(3):
        for i in range(4):
            await seg.handle_frame(make_frame(i))
        await seg._on_stt_event(event="results", transcript="segment", speech_final=True)

    check(len(segments) == 3, "three separate segments produced")
    check([s.sequence for s in segments] == [0, 1, 2], "sequence numbers increment correctly across segments")
    check(all(len(s.audio) == 4 * 640 for s in segments), "each segment's buffer resets correctly (no leftover audio between segments)")


async def test_stop_flushes_partial_segment_without_network() -> None:
    print("\ntest_stop_flushes_partial_segment_without_network")
    segments = []
    cfg = Config(deepgram_api_key=None)
    seg = Segmenter(cfg, "session-f", on_segment=lambda s: segments.append(s))

    for i in range(3):
        await seg.handle_frame(make_frame(i))
    # No trigger fired, then the session just... ends (teacher disconnects).
    await seg.stop()

    check(len(segments) == 1, "stop() flushes the in-progress partial segment")
    check(segments[0].trigger == "session_end", f"flushed segment's trigger is 'session_end' (got {segments[0].trigger!r})")


async def test_interim_results_are_not_accumulated() -> None:
    print("\ntest_interim_results_are_not_accumulated")
    # Regression test for a real bug: Deepgram sends interim (is_final=False)
    # Results messages repeatedly while still refining its guess at the same
    # stretch of speech. A real run against live audio showed these getting
    # accumulated alongside the final text, producing a transcript with the
    # same phrase repeated several times.
    segments = []
    cfg = Config(deepgram_api_key=None)
    seg = Segmenter(cfg, "session-g", on_segment=lambda s: segments.append(s))

    for i in range(5):
        await seg.handle_frame(make_frame(i))

    # Simulate Deepgram refining its guess: several interim drafts of the
    # same growing phrase, none of them final.
    await seg._on_stt_event(event="results", transcript="Gimana", is_final=False, speech_final=False)
    await seg._on_stt_event(event="results", transcript="Gimana ada", is_final=False, speech_final=False)
    await seg._on_stt_event(event="results", transcript="Gimana ada masalah", is_final=False, speech_final=False)
    # Then the actual finalized chunk arrives.
    await seg._on_stt_event(event="results", transcript="Gimana ada masalah?", is_final=True, speech_final=True)

    check(len(segments) == 1, "exactly one segment closed")
    check(
        segments[0].transcript == "Gimana ada masalah?",
        f"only the final transcript is kept, interim drafts discarded (got {segments[0].transcript!r})",
    )


async def test_force_close_finalizes_in_progress_segment() -> None:
    print("\ntest_force_close_finalizes_in_progress_segment")
    segments = []
    cfg = Config(deepgram_api_key=None)
    seg = Segmenter(cfg, "session-h", on_segment=lambda s: segments.append(s))

    for i in range(4):
        await seg.handle_frame(make_frame(i))
    # No speech_final, no UtteranceEnd — simulating a student interrupting
    # mid-sentence with a question.
    await seg.force_close()

    check(len(segments) == 1, "force_close produces exactly one segment")
    check(segments[0].trigger == "interrupted", f"trigger is 'interrupted' (got {segments[0].trigger!r})")
    check(len(segments[0].audio) == 4 * 640, "all audio captured so far is included, nothing discarded")


async def test_force_close_with_no_audio_is_noop() -> None:
    print("\ntest_force_close_with_no_audio_is_noop")
    segments = []
    cfg = Config(deepgram_api_key=None)
    seg = Segmenter(cfg, "session-i", on_segment=lambda s: segments.append(s))

    # No frames ever arrived (e.g. student interrupts right at session start).
    await seg.force_close()
    check(len(segments) == 0, "force_close on an empty segment produces nothing, not a bogus empty segment")


async def main() -> int:
    await test_speech_final_closes_segment()
    await test_utterance_end_is_fallback()
    await test_utterance_end_with_no_audio_is_noop()
    await test_max_duration_safety_net()
    await test_multiple_segments_increment_sequence()
    await test_stop_flushes_partial_segment_without_network()
    await test_interim_results_are_not_accumulated()
    await test_force_close_finalizes_in_progress_segment()
    await test_force_close_with_no_audio_is_noop()

    print()
    if FAILURES:
        print(f"{FAILURES} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
