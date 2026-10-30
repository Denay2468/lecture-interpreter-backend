#!/usr/bin/env python3
"""
Student test client — receives ordered translated lecture audio, and
can optionally simulate pressing Ask to ask a question, exercising the
full Question Mode path end to end.

Runs two things concurrently on one connection, matching how a real
student client works:
  1. A listen loop: receives translated lecture segments (paused
     server-side during Question Mode, per Rule 5) and control messages
     (mode_changed, question_delivered, question_failed).
  2. An optional Ask simulation: after a configurable delay, sends
     ask_press, streams a question WAV file as frames (same wire
     framing as the teacher direction), waits a short simulated
     post-roll, then sends ask_release.

Usage:
    # Just listen (original behavior):
    python tools/receive_student_audio.py

    # Also simulate asking a question 8 seconds after connecting:
    python tools/receive_student_audio.py \
        --question-audio sample_audio/my_question.wav \
        --ask-after-seconds 8
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets  # noqa: E402

from app.protocol import (  # noqa: E402
    decode_playback_frame,
    encode_frame,
    ProtocolError,
)

FRAME_MS = 20  # matches the teacher direction's framing


def _read_wav_frames(path: str, frame_ms: int) -> list[bytes]:
    with wave.open(path, "rb") as wf:
        sample_rate = wf.getframerate()
        sample_width = wf.getsampwidth()
        channels = wf.getnchannels()
        if sample_width != 2 or channels != 1:
            raise ValueError(
                f"Expected 16-bit mono PCM WAV, got sample_width={sample_width} "
                f"channels={channels}. Convert it first (see benchmark project's ffmpeg command)."
            )
        raw = wf.readframes(wf.getnframes())

    bytes_per_ms = int(sample_rate * sample_width / 1000)
    chunk_size = bytes_per_ms * frame_ms
    return [raw[i:i + chunk_size] for i in range(0, len(raw), chunk_size)]


async def listen_loop(ws, out_dir: Path, segments_event=None, segments_needed: int = 0) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    received_order: list[int] = []
    arrival_index = 0

    def order_report() -> str:
        if not received_order:
            return "no segments received"
        ok = received_order == sorted(received_order)
        verdict = "PASS" if ok else "FAIL (a real bug in Ordered Playback)"
        return f"order so far {received_order} -> {verdict}"

    try:
        async for message in ws:
            if isinstance(message, bytes):
                try:
                    sequence, audio = decode_playback_frame(message)
                except ProtocolError as e:
                    print(f"Skipping malformed frame: {e}")
                    continue
                received_order.append(sequence)
                path = out_dir / f"arrival{arrival_index:03d}_seq{sequence:03d}.mp3"
                path.write_bytes(audio)
                print(f"[lecture] segment #{sequence} ({len(audio)} bytes) -> {path.name}")
                print(f"  Order check: {order_report()}")
                arrival_index += 1
                if segments_event is not None and len(received_order) >= segments_needed:
                    segments_event.set()
            else:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue
                msg_type = data.get("type")
                if msg_type == "mode_changed":
                    print(f"\n[mode] >>> {data.get('mode').upper()} <<<\n")
                elif msg_type == "question_delivered":
                    print(f"\n[question] Delivered: {data.get('source_transcript')!r} "
                          f"-> {data.get('translated_text')!r}\n")
                elif msg_type == "question_failed":
                    print(f"\n[question] FAILED: {data.get('error')} "
                          f"(source: {data.get('source_transcript')!r})\n")
                else:
                    print(f"[control] {data}")
    except websockets.ConnectionClosed as e:
        if e.rcvd is not None:
            print(f"\n[closed by server] {e.rcvd.code}: {e.rcvd.reason}")
    finally:
        print(f"\nFinal: received {len(received_order)} lecture segment(s). "
              f"Order check: {order_report()}")


async def ask_question(ws, question_audio_path: str, ask_after_seconds: float, post_roll_ms: float,
                       segments_event=None) -> None:
    if segments_event is not None:
        # Relative trigger: press as soon as N lecture segments have arrived. Hand-timing
        # --ask-after-seconds against a teacher you start by hand kept missing the window
        # where a segment is mid-flight, because attach time and start delay vary per run.
        await segments_event.wait()
        print("\n[ask] Enough lecture segments received - pressing Ask now (ignoring --ask-after-seconds)")
    else:
        await asyncio.sleep(ask_after_seconds)

    chunks = _read_wav_frames(question_audio_path, FRAME_MS)
    print(f"\n[ask] Pressing Ask — streaming {question_audio_path} ({len(chunks)} frames)...")

    await ws.send(json.dumps({"type": "ask_press"}))

    start = time.monotonic()
    frame_s = FRAME_MS / 1000.0
    for seq, chunk in enumerate(chunks):
        # Schedule-based pacing (see send_test_audio.py for why): Windows
        # rounds asyncio.sleep(0.02) up to ~31ms, so per-frame sleeps made
        # a 4.6s question take ~7.2s to send.
        delay = (start + seq * frame_s) - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        capture_ts_ms = int((time.monotonic() - start) * 1000)
        await ws.send(encode_frame(seq, capture_ts_ms, chunk))

    print(f"[ask] Done streaming question audio. Waiting {post_roll_ms:.0f}ms (simulated post-roll) before releasing...")
    await asyncio.sleep(post_roll_ms / 1000.0)

    await ws.send(json.dumps({"type": "ask_release"}))
    print("[ask] Released Ask. Waiting for the answer...\n")


async def main_async(uri: str, out_dir: Path, question_audio, ask_after_seconds: float, post_roll_ms: float,
                     ask_after_segments: int = 0) -> None:
    print(f"Connecting to {uri} ...")
    async with websockets.connect(uri, max_size=None) as ws:
        print("Connected. Listening for segments (Ctrl+C to stop)...\n")

        segments_event = asyncio.Event() if ask_after_segments > 0 else None
        tasks = [asyncio.create_task(listen_loop(ws, out_dir, segments_event, ask_after_segments))]
        if question_audio:
            tasks.append(asyncio.create_task(
                ask_question(ws, question_audio, ask_after_seconds, post_roll_ms, segments_event)))

        try:
            await asyncio.gather(*tasks)
        except websockets.ConnectionClosed:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--out-dir", default="received_audio")
    parser.add_argument("--question-audio", default=None,
                         help="Path to a 16kHz mono PCM16 WAV of an English question. "
                              "If given, Ask is simulated automatically.")
    parser.add_argument("--ask-after-seconds", type=float, default=8.0,
                         help="Delay after connecting before simulating the Ask press (default: 8.0)")
    parser.add_argument("--post-roll-ms", type=float, default=300.0,
                         help="Simulated delay between the last audio frame and ask_release (default: 300)")
    parser.add_argument("--ask-after-segments", type=int, default=0,
                         help="Press Ask right after this many lecture segments have been received, "
                              "instead of after a fixed delay (overrides --ask-after-seconds). "
                              "2 lands mid-sentence in the third segment of the test lecture.")
    parser.add_argument("--code", default=None, help="Room join code. Needed when the server runs with a database.")
    parser.add_argument("--token", default=None, help="Student access token (or set STUDENT_TOKEN). Needed with --code.")
    args = parser.parse_args()

    uri = f"ws://{args.host}:{args.port}/ws/student"
    if args.code:
        import os
        uri += f"?code={args.code}&token={args.token or os.getenv('STUDENT_TOKEN', '')}"
    try:
        asyncio.run(main_async(uri, Path(args.out_dir), args.question_audio,
                                args.ask_after_seconds, args.post_roll_ms,
                                args.ask_after_segments))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
