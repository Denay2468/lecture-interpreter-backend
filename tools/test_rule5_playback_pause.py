#!/usr/bin/env python3
"""
Rule 5 against the REAL server: while a student holds Ask, finished
translations must not be sent; they must arrive, in order, after release.
No Deepgram/DeepL/ElevenLabs involved: a playback controller is attached
to the real Server and finished segments are injected straight into it.

Each cycle injects a segment the instant the student is told "question".
That targets a race: the send loop notices Question Mode by polling every
50ms, so a segment landing right after a press can win and leak through.

Usage:
    python tools/test_rule5_playback_pause.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets  # noqa: E402

from app.config import Config  # noqa: E402
from app.playback import StudentPlaybackController  # noqa: E402
from app.protocol import decode_playback_frame  # noqa: E402
from app.server import LEGACY_KEY, Server  # noqa: E402
from app.translation_pipeline import TranslatedSegment  # noqa: E402

PORT = 8793
FAILURES = 0
QUESTION = ("ctrl", {"type": "mode_changed", "mode": "question"})


def check(condition: bool, description: str) -> None:
    global FAILURES
    print(f"  [{'PASS' if condition else 'FAIL'}] {description}")
    if not condition:
        FAILURES += 1


def seg(seq: int) -> TranslatedSegment:
    return TranslatedSegment(session_id="t", sequence=seq, source_transcript=f"src{seq}",
                             translated_text=f"tr{seq}", audio=b"mp3" + bytes([seq]), state="delivered")


async def recv_item(ws, timeout: float, keep_captions: bool = False):
    while True:
        msg = await asyncio.wait_for(ws.recv(), timeout)
        if isinstance(msg, bytes):
            return ("audio", decode_playback_frame(msg)[0])
        data = json.loads(msg)
        if data.get("type") == "segment" and not keep_captions:
            continue  # the caption that precedes every audio frame; checked once below
        return ("ctrl", data)


async def drain_until(ws, wanted, timeout: float = 3.0) -> list:
    seen: list = []
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        item = await recv_item(ws, max(0.01, deadline - asyncio.get_running_loop().time()))
        seen.append(item)
        if item == wanted:
            return seen


async def silence_or_item(ws, seconds: float):
    try:
        return await recv_item(ws, seconds)
    except asyncio.TimeoutError:
        return None


async def press(ws) -> None:
    await ws.send(json.dumps({"type": "ask_press"}))
    await drain_until(ws, QUESTION)


async def release(ws) -> None:
    await ws.send(json.dumps({"type": "ask_release"}))


async def main() -> int:
    server = Server(Config())
    playback = StudentPlaybackController("t")
    server._active[LEGACY_KEY].coordinator._playback = playback  # stands in for an attached teacher session

    async with websockets.serve(server._router, "localhost", PORT):
        async with websockets.connect(f"ws://localhost:{PORT}/ws/student") as ws:
            print("\ntest_lecture_mode_delivers_normally")
            check(await recv_item(ws, 3) == ("ctrl", {"type": "mode_changed", "mode": "lecture"}), "told 'lecture' on connect")
            playback.submit(seg(0))
            check(await recv_item(ws, 2, keep_captions=True) == ("ctrl", {"type": "segment", "sequence": 0, "source_transcript": "src0", "translated_text": "tr0"}),
                  "each segment's text arrives just before its audio, for captions")
            check(await recv_item(ws, 2) == ("audio", 0), "a finished segment is delivered promptly in Lecture Mode")

            print("\ntest_segment_finishing_right_after_press_is_held (5 cycles)")
            n = 1
            for cycle in range(5):
                await press(ws)
                playback.submit(seg(n))  # lands inside the 50ms polling window
                got = await silence_or_item(ws, 0.4)
                held = got is None
                check(held, f"cycle {cycle}: segment #{n} held during Question Mode" + ("" if held else f" (LEAKED: {got})"))
                await release(ws)
                if not held and got == ("audio", n):
                    n += 1
                    continue
                try:
                    await drain_until(ws, ("audio", n))
                    check(True, f"cycle {cycle}: segment #{n} delivered after release")
                except asyncio.TimeoutError:
                    check(False, f"cycle {cycle}: segment #{n} never delivered after release")
                n += 1

            print("\ntest_held_segments_arrive_in_order_after_release")
            a, b = n, n + 1
            await press(ws)
            playback.submit(seg(b))  # finishes first, but must wait for a
            playback.submit(seg(a))
            check(await silence_or_item(ws, 0.4) is None, "nothing sent during Question Mode")
            await release(ws)
            try:
                seen = await drain_until(ws, ("audio", b))
                order = [s for kind, s in seen if kind == "audio"]
                check(order == [a, b], f"delivered in speaking order after release (got {order})")
            except asyncio.TimeoutError:
                check(False, "held segments never delivered after release")

    print()
    if FAILURES:
        print(f"{FAILURES} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
