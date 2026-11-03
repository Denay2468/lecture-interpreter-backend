#!/usr/bin/env python3
"""
Local test of the Question Pipeline's retry/failure logic — no
Deepgram/DeepL/ElevenLabs calls, no network, no API keys required.

Verifies Rule 8 specifically: a failed question must come back as a
clear, visible failure (not silently dropped the way a failed lecture
segment is per Rule 7), and that one retry happens per stage before
giving up.

Usage:
    python tools/test_question_pipeline_logic.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.question_pipeline import process_question  # noqa: E402

FAILURES = 0


def check(condition: bool, description: str) -> None:
    global FAILURES
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {description}")
    if not condition:
        FAILURES += 1


async def test_successful_question() -> None:
    print("\ntest_successful_question")

    async def fake_transcribe(audio: bytes) -> str:
        return "What is the difference between a container and a VM?"

    async def fake_translate(text: str) -> str:
        return "[ID] " + text

    async def fake_synthesize(text: str) -> bytes:
        return b"fake-indonesian-audio"

    cfg = Config()
    result = await process_question(
        cfg, "session-x", b"fake-english-audio",
        transcribe_fn=fake_transcribe, translate_fn=fake_translate, synthesize_fn=fake_synthesize,
    )

    check(result.success, "question succeeds end to end")
    check(result.source_transcript == "What is the difference between a container and a VM?", "transcript recorded")
    check(result.translated_text.startswith("[ID]"), "translation recorded")
    check(result.audio == b"fake-indonesian-audio", "audio recorded")
    check(result.error is None, "no error on success")


async def test_stt_fails_twice_is_visible_failure() -> None:
    print("\ntest_stt_fails_twice_is_visible_failure")
    call_count = {"n": 0}

    async def always_fails(audio: bytes) -> str:
        call_count["n"] += 1
        raise RuntimeError("Deepgram unreachable")

    cfg = Config()
    result = await process_question(cfg, "session-x", b"audio", transcribe_fn=always_fails)

    check(call_count["n"] == 2, "STT was retried once (2 total attempts)")
    check(not result.success, "result is a failure")
    check("Speech recognition failed" in (result.error or ""), f"error names the STT stage (got {result.error!r})")
    check(result.audio is None, "no audio on a failed question")


async def test_empty_transcript_is_treated_as_failure() -> None:
    print("\ntest_empty_transcript_is_treated_as_failure")

    async def empty_transcribe(audio: bytes) -> str:
        return ""  # e.g. background noise, nothing recognizable as speech

    cfg = Config()
    result = await process_question(cfg, "session-x", b"audio", transcribe_fn=empty_transcribe)

    check(not result.success, "an empty transcript is a failure, not a silent no-op (unlike a silent lecture segment)")
    check("no speech detected" in (result.error or ""), f"error is specific about why (got {result.error!r})")


async def test_mt_retry_then_succeed() -> None:
    print("\ntest_mt_retry_then_succeed")
    call_count = {"n": 0}

    async def fake_transcribe(audio: bytes) -> str:
        return "hello"

    async def flaky_translate(text: str) -> str:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient DeepL error")
        return "halo"

    async def fake_synthesize(text: str) -> bytes:
        return b"audio"

    cfg = Config()
    result = await process_question(
        cfg, "session-x", b"audio",
        transcribe_fn=fake_transcribe, translate_fn=flaky_translate, synthesize_fn=fake_synthesize,
    )

    check(call_count["n"] == 2, "MT was retried after the first failure")
    check(result.success and result.translated_text == "halo", "question succeeds once the retry works")


async def test_tts_failure_preserves_transcript_and_translation() -> None:
    print("\ntest_tts_failure_preserves_transcript_and_translation")

    async def fake_transcribe(audio: bytes) -> str:
        return "hello"

    async def fake_translate(text: str) -> str:
        return "halo"

    async def always_fails(text: str) -> bytes:
        raise RuntimeError("ElevenLabs unreachable")

    cfg = Config()
    result = await process_question(
        cfg, "session-x", b"audio",
        transcribe_fn=fake_transcribe, translate_fn=fake_translate, synthesize_fn=always_fails,
    )

    check(not result.success, "TTS failure is a failure")
    check("Speech synthesis failed" in (result.error or ""), "error names the TTS stage")
    check(result.source_transcript == "hello", "transcript still preserved even though the question ultimately failed")
    check(result.translated_text == "halo", "translation still preserved too — only the audio is missing")


async def main() -> int:
    await test_successful_question()
    await test_stt_fails_twice_is_visible_failure()
    await test_empty_transcript_is_treated_as_failure()
    await test_mt_retry_then_succeed()
    await test_tts_failure_preserves_transcript_and_translation()

    print()
    if FAILURES:
        print(f"{FAILURES} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
