"""Mode State Machine — architecture spec Section 3, Rules 1, 2, 12.

Deliberately pure and side-effect-free: just tracks Lecture <-> Question
mode and the press-duration debounce decision. All the actual side
effects of a mode change (gating teacher audio, pausing student
playback, force-closing the in-progress segment, broadcasting to
clients) live in SessionCoordinator, which uses this class rather than
duplicating its logic. Keeping this piece separate is what makes it
unit-testable without needing a real Deepgram/DeepL/ElevenLabs
connection — see tools/test_mode_state_logic.py.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


def _now_ms() -> float:
    return time.monotonic() * 1000.0


@dataclass
class PressResult:
    """What release() tells the caller to do."""
    held_ms: float
    is_real_press: bool  # False => Rule 12: too short, treat as noise


class ModeStateMachine:
    """Single-session mode state. Rule 1: exactly one of two states,
    ever — there's no third "transitioning" state visible externally,
    the switch is instant (Rule 2)."""

    LECTURE = "lecture"
    QUESTION = "question"

    def __init__(self, min_press_duration_ms: int) -> None:
        self._min_press_duration_ms = min_press_duration_ms
        self.mode = self.LECTURE
        self._press_started_at: float | None = None

    @property
    def is_question_mode(self) -> bool:
        return self.mode == self.QUESTION

    def press(self) -> None:
        """Rule 2: instant switch the moment the button is pressed —
        no waiting for anything in-flight to finish first. Safe to call
        even if already in Question Mode (treated as a no-op on the
        mode itself, but resets the press timer — see test for the
        double-press case)."""
        self.mode = self.QUESTION
        self._press_started_at = _now_ms()

    def release(self) -> PressResult:
        """Rule 2: instant switch back. Rule 12: also reports whether
        this press was long enough to be treated as a real question —
        the caller (SessionCoordinator) uses this to decide whether to
        actually process the captured audio or discard it as noise.
        The mode itself always flips back to Lecture regardless —
        debouncing affects whether the *question* is processed, not
        whether the mode switch happens."""
        now = _now_ms()
        held_ms = now - (self._press_started_at or now)
        self.mode = self.LECTURE
        self._press_started_at = None
        return PressResult(held_ms=held_ms, is_real_press=held_ms >= self._min_press_duration_ms)
