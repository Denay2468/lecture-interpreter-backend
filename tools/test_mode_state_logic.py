#!/usr/bin/env python3
"""
Local test of the Mode State Machine — no network, no API keys.

Usage:
    python tools/test_mode_state_logic.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.mode_state import ModeStateMachine  # noqa: E402

FAILURES = 0


def check(condition: bool, description: str) -> None:
    global FAILURES
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {description}")
    if not condition:
        FAILURES += 1


def test_starts_in_lecture_mode() -> None:
    print("\ntest_starts_in_lecture_mode")
    m = ModeStateMachine(min_press_duration_ms=300)
    check(m.mode == ModeStateMachine.LECTURE, "starts in Lecture Mode")
    check(not m.is_question_mode, "is_question_mode is False initially")


def test_press_switches_instantly() -> None:
    print("\ntest_press_switches_instantly")
    m = ModeStateMachine(min_press_duration_ms=300)
    m.press()
    check(m.mode == ModeStateMachine.QUESTION, "mode is Question immediately after press(), no delay")
    check(m.is_question_mode, "is_question_mode is True")


def test_long_press_is_a_real_question() -> None:
    print("\ntest_long_press_is_a_real_question")
    m = ModeStateMachine(min_press_duration_ms=50)
    m.press()
    time.sleep(0.08)  # 80ms, well over the 50ms threshold
    result = m.release()
    check(m.mode == ModeStateMachine.LECTURE, "mode returns to Lecture on release")
    check(result.is_real_press, f"press held {result.held_ms:.0f}ms, >= 50ms threshold => treated as real")


def test_short_press_is_debounced() -> None:
    print("\ntest_short_press_is_debounced")
    # Rule 12: a very short press is noise, not a question.
    m = ModeStateMachine(min_press_duration_ms=300)
    m.press()
    result = m.release()  # released almost instantly, well under 300ms
    check(m.mode == ModeStateMachine.LECTURE, "mode STILL returns to Lecture even on a debounced press")
    check(not result.is_real_press, f"press held {result.held_ms:.1f}ms, < 300ms threshold => debounced, not a real question")


def test_release_without_press_does_not_crash() -> None:
    print("\ntest_release_without_press_does_not_crash")
    # Defensive case: shouldn't happen via a well-behaved client, but a
    # stray/duplicate release message shouldn't blow up the server.
    m = ModeStateMachine(min_press_duration_ms=300)
    result = m.release()
    check(m.mode == ModeStateMachine.LECTURE, "stays in Lecture Mode")
    check(isinstance(result.held_ms, float), "returns a sane result rather than raising")


def test_rapid_press_release_cycles_are_deterministic() -> None:
    print("\ntest_rapid_press_release_cycles_are_deterministic")
    m = ModeStateMachine(min_press_duration_ms=10)
    results = []
    for _ in range(5):
        m.press()
        check(m.mode == ModeStateMachine.QUESTION, "each press enters Question Mode")
        time.sleep(0.02)
        r = m.release()
        check(m.mode == ModeStateMachine.LECTURE, "each release returns to Lecture Mode")
        results.append(r.is_real_press)
    check(all(results), "every cycle in this test held long enough to count as real (sanity check on timing)")


def main() -> int:
    test_starts_in_lecture_mode()
    test_press_switches_instantly()
    test_long_press_is_a_real_question()
    test_short_press_is_debounced()
    test_release_without_press_does_not_crash()
    test_rapid_press_release_cycles_are_deterministic()

    print()
    if FAILURES:
        print(f"{FAILURES} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
