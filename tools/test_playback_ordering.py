#!/usr/bin/env python3
"""
Local test of Ordered Student Playback's reordering logic — no network,
no API keys required.

Feeds TranslatedSegment results into StudentPlaybackController
deliberately out of order (simulating what concurrent Translation Queue
workers can actually produce) and verifies they come back out via
get_next() in strict original sequence order — which is the entire
point of this component.

Usage:
    python tools/test_playback_ordering.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.playback import StudentPlaybackController  # noqa: E402
from app.translation_pipeline import TranslatedSegment  # noqa: E402

FAILURES = 0


def check(condition: bool, description: str) -> None:
    global FAILURES
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {description}")
    if not condition:
        FAILURES += 1


def make_result(seq: int, state: str = "delivered", audio: bytes = b"audio") -> TranslatedSegment:
    return TranslatedSegment(
        session_id="test-session", sequence=seq,
        source_transcript=f"source {seq}", translated_text=f"translated {seq}",
        audio=audio if state == "delivered" else None,
        state=state, error=None,
    )


async def test_in_order_submission_plays_in_order() -> None:
    print("\ntest_in_order_submission_plays_in_order")
    controller = StudentPlaybackController("test-session")
    for i in range(4):
        controller.submit(make_result(i))

    played = [await controller.get_next() for _ in range(4)]
    check([r.sequence for r in played] == [0, 1, 2, 3], "plays back in submission order when already in order")


async def test_out_of_order_submission_still_plays_in_order() -> None:
    print("\ntest_out_of_order_submission_still_plays_in_order")
    # This is the actual scenario that matters: concurrent translation
    # workers finish in whatever order the APIs happen to respond.
    controller = StudentPlaybackController("test-session")
    for i in [2, 0, 3, 1]:  # deliberately scrambled arrival order
        controller.submit(make_result(i))

    played = [await controller.get_next() for _ in range(4)]
    check(
        [r.sequence for r in played] == [0, 1, 2, 3],
        f"reordered back to correct sequence despite scrambled arrival (got {[r.sequence for r in played]})",
    )


async def test_later_segment_waits_for_earlier_one() -> None:
    print("\ntest_later_segment_waits_for_earlier_one")
    controller = StudentPlaybackController("test-session")
    controller.submit(make_result(1))  # arrives first, but it's segment #1

    check(controller.pending_count() == 0, "segment #1 is held back — segment #0 hasn't arrived yet")

    controller.submit(make_result(0))  # segment #0 finally arrives
    check(controller.pending_count() == 2, "both #0 and #1 release together, in order, once #0 arrives")

    first = await controller.get_next()
    second = await controller.get_next()
    check(first.sequence == 0 and second.sequence == 1, "released in correct order: 0 then 1")


async def test_skipped_silent_segments_advance_without_playing() -> None:
    print("\ntest_skipped_silent_segments_advance_without_playing")
    controller = StudentPlaybackController("test-session")
    controller.submit(make_result(0, state="delivered"))
    controller.submit(make_result(1, state="skipped"))     # silence, or a real failure — either way, nothing to play
    controller.submit(make_result(2, state="delivered"))

    played = []
    played.append(await controller.get_next())
    played.append(await controller.get_next())

    check(len(played) == 2, "only the 2 delivered segments are ever handed out for playback")
    check([r.sequence for r in played] == [0, 2], "segment #1 (skipped) is absorbed, not played, but didn't block #2")
    check(controller.segments_played == 2, "segments_played counts only actual playback")
    check(controller.segments_silently_advanced == 1, "segments_silently_advanced counts the one skipped segment")


async def test_mixed_scramble_with_skips() -> None:
    print("\ntest_mixed_scramble_with_skips")
    # A more realistic combined scenario: out-of-order arrival AND some
    # skipped/silent segments mixed in.
    controller = StudentPlaybackController("test-session")
    results = {
        0: make_result(0, state="delivered"),
        1: make_result(1, state="skipped"),
        2: make_result(2, state="delivered"),
        3: make_result(3, state="skipped"),
        4: make_result(4, state="delivered"),
    }
    for i in [2, 4, 0, 3, 1]:  # scrambled arrival
        controller.submit(results[i])

    played = []
    while controller.pending_count() > 0:
        played.append(await controller.get_next())

    check(
        [r.sequence for r in played] == [0, 2, 4],
        f"only delivered segments played, in correct order despite scrambled arrival + skips (got {[r.sequence for r in played]})",
    )


async def main() -> int:
    await test_in_order_submission_plays_in_order()
    await test_out_of_order_submission_still_plays_in_order()
    await test_later_segment_waits_for_earlier_one()
    await test_skipped_silent_segments_advance_without_playing()
    await test_mixed_scramble_with_skips()

    print()
    if FAILURES:
        print(f"{FAILURES} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
