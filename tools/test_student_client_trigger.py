#!/usr/bin/env python3
"""
Local test of the student client's --ask-after-segments trigger. A fake
server sends three playback segments 150ms apart and records what the
client does; no real server, no API keys.

Checks that the client presses Ask only AFTER it has received the
requested number of segments, and that the fixed --ask-after-seconds delay
(deliberately set to 30s here) is ignored when the segment trigger is used.

Usage:
    python tools/test_student_client_trigger.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import websockets  # noqa: E402

import receive_student_audio as client  # noqa: E402
from app.protocol import encode_playback_frame  # noqa: E402

FAILURES = 0


def check(condition: bool, description: str) -> None:
    global FAILURES
    print(f"  [{'PASS' if condition else 'FAIL'}] {description}")
    if not condition:
        FAILURES += 1


def write_silence_wav(path: Path, ms: int = 100) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * int(16000 * ms / 1000))


async def run_case(port: int, segments_needed: int) -> list:
    events: list = []

    async def handler(ws) -> None:
        async def sender() -> None:
            for seq in range(3):
                await asyncio.sleep(0.15)
                events.append(("sending", seq))  # recorded before the send, so ordering is trustworthy
                await ws.send(encode_playback_frame(seq, b"fake-mp3"))

        send_task = asyncio.create_task(sender())
        async for msg in ws:
            if isinstance(msg, str):
                kind = json.loads(msg)["type"]
                events.append(("recv", kind))
                if kind == "ask_release":
                    break
        await send_task
        await ws.close()

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "q.wav"
        write_silence_wav(wav)
        async with websockets.serve(handler, "localhost", port):
            await asyncio.wait_for(
                client.main_async(f"ws://localhost:{port}/ws/student", Path(tmp) / "out",
                                  str(wav), 30.0, 10.0, segments_needed),
                timeout=10,
            )
    return events


async def main() -> int:
    for port, n in ((8791, 2), (8792, 3)):
        print(f"\ntest_ask_waits_for_{n}_segments")
        try:
            events = await run_case(port, n)
        except asyncio.TimeoutError:
            check(False, "client finished within 10s (it probably waited on the 30s fixed delay instead)")
            continue
        check(("recv", "ask_press") in events and ("recv", "ask_release") in events, "client pressed and released Ask")
        if ("recv", "ask_press") in events:
            check(events.index(("recv", "ask_press")) > events.index(("sending", n - 1)),
                  f"Ask was pressed only after segment #{n - 1} (the {n}th) had been sent")
        check(True, "the 30s --ask-after-seconds delay was ignored (the case finished well inside the 10s timeout)")

    print()
    if FAILURES:
        print(f"{FAILURES} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
