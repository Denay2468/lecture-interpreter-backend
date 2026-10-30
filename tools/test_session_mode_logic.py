#!/usr/bin/env python3
"""
Local test of SessionCoordinator's Question Mode handling: mode state, the
student-disconnect guard, and the streaming-STT lifecycle (started at press,
fed live, aborted on discard, handed to the pipeline on a real release).
No network or API keys: the transcriber factory and the pipeline are fakes.

Usage:
    python tools/test_session_mode_logic.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.protocol import encode_frame  # noqa: E402
from app.session import SessionCoordinator  # noqa: E402

FAILURES = 0
PAYLOAD = b"\x00\x01" * 320  # 640 bytes
QUESTION_FRAME = encode_frame(0, 0, PAYLOAD)


def check(condition: bool, description: str) -> None:
    global FAILURES
    print(f"  [{'PASS' if condition else 'FAIL'}] {description}")
    if not condition:
        FAILURES += 1


class FakeTranscriber:
    def __init__(self, cfg, session_id):
        self.started = False
        self.fed: list[bytes] = []
        self.aborted = False

    def start(self) -> None:
        self.started = True

    def feed(self, payload: bytes) -> None:
        self.fed.append(payload)

    async def abort(self) -> None:
        self.aborted = True

    async def finish(self) -> str:
        return "unused"


def make_coordinator(**cfg_overrides):
    transcribers: list[FakeTranscriber] = []
    teacher_msgs: list[dict] = []
    processed: list[tuple] = []

    def factory(cfg, session_id):
        t = FakeTranscriber(cfg, session_id)
        transcribers.append(t)
        return t

    c = SessionCoordinator(Config(**cfg_overrides), transcriber_factory=factory)

    async def teacher_audio(_audio: bytes) -> None:
        pass

    async def teacher_control(msg: dict) -> None:
        teacher_msgs.append(msg)

    async def fake_process(audio: bytes, transcriber=None) -> None:
        processed.append((audio, transcriber))

    c.register_teacher_sender(teacher_audio, teacher_control)
    c._process_and_deliver_question = fake_process  # replaces the real STT/MT/TTS chain
    return c, teacher_msgs, processed, transcribers


async def test_current_mode_reflects_state() -> None:
    print("\ntest_current_mode_reflects_state")
    c, _, _, _ = make_coordinator()
    check(c.current_mode == "lecture", "starts as 'lecture'")
    await c.handle_ask_press()
    check(c.current_mode == "question", "'question' while Ask is held")
    await c.handle_ask_release()
    check(c.current_mode == "lecture", "back to 'lecture' after release")


async def test_press_starts_streaming_and_feeds_frames() -> None:
    print("\ntest_press_starts_streaming_stt_and_feeds_frames")
    c, _, _, transcribers = make_coordinator()
    await c.handle_ask_press()
    check(len(transcribers) == 1 and transcribers[0].started, "a transcriber is created and started at press")
    c.handle_question_audio_frame(QUESTION_FRAME)
    c.handle_question_audio_frame(QUESTION_FRAME)
    check(transcribers[0].fed == [PAYLOAD, PAYLOAD], "each question frame is forwarded live")


async def test_short_press_aborts_streaming() -> None:
    print("\ntest_short_press_aborts_streaming_stt")
    c, _, processed, transcribers = make_coordinator(question_min_duration_ms=300)
    await c.handle_ask_press()
    c.handle_question_audio_frame(QUESTION_FRAME)
    await c.handle_ask_release()  # released almost instantly: accidental
    await asyncio.sleep(0.02)
    check(transcribers[0].aborted, "streaming STT aborted for an accidental press")
    check(processed == [], "nothing sent through the pipeline")


async def test_release_without_audio_aborts_streaming() -> None:
    print("\ntest_release_with_no_audio_aborts_streaming_stt")
    c, _, processed, transcribers = make_coordinator(question_min_duration_ms=10)
    await c.handle_ask_press()
    await asyncio.sleep(0.05)
    await c.handle_ask_release()
    await asyncio.sleep(0.02)
    check(transcribers[0].aborted and processed == [], "aborted, nothing processed")


async def test_real_release_hands_transcriber_to_pipeline() -> None:
    print("\ntest_real_release_hands_the_transcriber_to_the_pipeline")
    c, _, processed, transcribers = make_coordinator(question_min_duration_ms=10)
    await c.handle_ask_press()
    c.handle_question_audio_frame(QUESTION_FRAME)
    await asyncio.sleep(0.05)
    await c.handle_ask_release()
    await asyncio.sleep(0.02)
    check(len(processed) == 1 and len(processed[0][0]) == 640, "question audio sent through the pipeline once")
    check(processed and processed[0][1] is transcribers[0], "the same transcriber is handed over for finish()")
    check(not transcribers[0].aborted, "not aborted on a real question")
    check(c.current_mode == "lecture", "Lecture Mode after release")


async def test_disconnect_mid_question() -> None:
    print("\ntest_disconnect_mid_question_reverts_to_lecture_and_aborts_streaming")
    c, teacher_msgs, processed, transcribers = make_coordinator()
    await c.handle_ask_press()
    c.handle_question_audio_frame(QUESTION_FRAME)
    check(c.is_question_mode and not c.lecture_mode_event.is_set(), "precondition: Question Mode, playback paused")
    teacher_msgs.clear()

    await c.handle_student_disconnect()
    await asyncio.sleep(0.02)

    check(not c.is_question_mode, "reverted to Lecture Mode")
    check(c.lecture_mode_event.is_set(), "lecture playback un-paused (Rule 5)")
    check(teacher_msgs == [{"type": "mode_changed", "mode": "lecture"}], f"teacher told (Rule 11), got {teacher_msgs}")
    check(len(c._question_audio) == 0, "partial question audio cleared")
    check(processed == [], "partial question NOT sent through the pipeline")
    check(transcribers[0].aborted, "streaming STT aborted (no Deepgram connection left open)")


async def test_disconnect_in_lecture_mode_is_a_noop() -> None:
    print("\ntest_disconnect_in_lecture_mode_is_a_noop")
    c, teacher_msgs, processed, transcribers = make_coordinator()
    await c.handle_student_disconnect()
    check(c.current_mode == "lecture" and teacher_msgs == [] and processed == [] and transcribers == [],
          "nothing happens, no spurious broadcast")


async def main() -> int:
    await test_current_mode_reflects_state()
    await test_press_starts_streaming_and_feeds_frames()
    await test_short_press_aborts_streaming()
    await test_release_without_audio_aborts_streaming()
    await test_real_release_hands_transcriber_to_pipeline()
    await test_disconnect_mid_question()
    await test_disconnect_in_lecture_mode_is_a_noop()
    print()
    if FAILURES:
        print(f"{FAILURES} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
