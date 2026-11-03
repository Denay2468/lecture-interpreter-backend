#!/usr/bin/env python3
"""
Test client for the teacher side of the pipeline — Teacher Audio
Ingest, Jitter Buffer, Segmentation, and now Question Mode's incoming
direction too.

Streams a WAV file to the server's /ws/teacher endpoint as sequenced,
timestamped frames — simulating a live teacher mic. Can optionally
inject simulated network jitter and packet loss, so you can actually
watch the Jitter Buffer do its job (reordering, gap-handling) instead
of just trusting it in the abstract.

Also now listens concurrently for whatever the server sends back: a
delivered question's synthesized Indonesian audio (saved to disk so you
can listen to it), and mode_changed control messages — matching how the
real teacher client is bidirectional (it needs to know when a student's
question has interrupted the lecture, and play the translated question
to the teacher).

Usage:
    python tools/send_test_audio.py sample_audio/lecture_sample_id_short.wav

    # With simulated bad network conditions:
    python tools/send_test_audio.py sample_audio/lecture_sample_id_short.wav \\
        --jitter-ms 80 --drop-rate 0.05 --reorder-rate 0.1

After running, check debug_output/<session_id>_smoothed.wav on the
server side — it should be a clean, in-order copy of the input audio
even when jitter/drop/reorder are cranked up, as long as they're within
what jitter_target_delay_ms / jitter_max_wait_ms are configured to
tolerate (see .env / app/config.py).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
import uuid
import wave
from pathlib import Path

# Allow running this script directly from tools/ without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets  # noqa: E402

from app.protocol import encode_frame  # noqa: E402

FRAME_MS = 20  # matches Config.target_frame_ms default


def _read_wav_frames(path: str, frame_ms: int) -> tuple[list[bytes], int]:
    with wave.open(path, "rb") as wf:
        sample_rate = wf.getframerate()
        sample_width = wf.getsampwidth()
        channels = wf.getnchannels()
        if sample_width != 2 or channels != 1:
            raise ValueError(
                f"Expected 16-bit mono PCM WAV, got sample_width={sample_width} channels={channels}. "
                "Convert it first (see the benchmark project's ffmpeg command in its README)."
            )
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    bytes_per_ms = int(sample_rate * sample_width / 1000)
    chunk_size = bytes_per_ms * frame_ms
    chunks = [raw[i:i + chunk_size] for i in range(0, len(raw), chunk_size)]
    return chunks, sample_rate


async def _listen_loop(ws, out_dir: Path) -> None:
    """Runs concurrently with sending. Saves any incoming question
    audio to disk and prints mode_changed events, so this test client
    reflects what a real (bidirectional) teacher client needs to do."""
    out_dir.mkdir(parents=True, exist_ok=True)
    received = 0
    try:
        async for message in ws:
            if isinstance(message, bytes):
                received += 1
                path = out_dir / f"question_answer_{received:03d}.mp3"
                try:
                    path.write_bytes(message)
                    print(f"\n[received] Question answer audio ({len(message)} bytes) -> saved as {path.name}")
                except OSError as e:
                    # Seen once on Windows as a transient Errno 22 during
                    # cleanup/cancellation — root cause unconfirmed
                    # (possibly antivirus briefly locking a newly created
                    # file). This at least stops it from crashing the
                    # whole test client and losing the fact that the
                    # audio WAS received.
                    print(f"\n[received] Question answer audio ({len(message)} bytes) "
                          f"— but failed to save to {path.name}: {e}")
            else:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "mode_changed":
                    print(f"\n[mode] Server says mode is now: {data.get('mode')}")
                else:
                    print(f"\n[control] {data}")
    except websockets.ConnectionClosed:
        pass


async def stream_audio(
    uri: str,
    audio_path: str,
    frame_ms: int,
    jitter_ms: int,
    drop_rate: float,
    reorder_rate: float,
    drain_seconds: float,
) -> None:
    chunks, sample_rate = _read_wav_frames(audio_path, frame_ms)
    print(f"Loaded {audio_path}: {len(chunks)} frames of {frame_ms}ms each, {sample_rate}Hz")

    async with websockets.connect(uri, max_size=None) as ws:
        print("Connected to " + re.sub(r"token=[^&]+", "token=***", uri))

        listen_task = asyncio.create_task(_listen_loop(ws, Path("received_audio")))

        start = time.monotonic()

        # Build the send order up front so we can simulate reordering
        # by literally sending frames out of sequence (their sequence
        # number in the header still reflects true order — that's what
        # lets the jitter buffer detect and fix the reordering).
        send_order = list(range(len(chunks)))
        if reorder_rate > 0:
            for i in range(len(send_order) - 1):
                if random.random() < reorder_rate:
                    send_order[i], send_order[i + 1] = send_order[i + 1], send_order[i]

        sent = 0
        dropped = 0

        frame_s = frame_ms / 1000.0
        for idx, seq in enumerate(send_order):
            # Schedule-based pacing: frame idx goes out at start + idx*frame_s,
            # not "sleep frame_s after the previous send". On Windows,
            # asyncio.sleep(0.02) rounds up to two ~15.6ms timer ticks
            # (~31ms), so per-frame sleeps ran this client at ~64% of real
            # time. Sleeping until an absolute target lets later frames
            # catch up, so the average rate stays at frame_ms.
            delay = (start + idx * frame_s) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)

            if drop_rate > 0 and random.random() < drop_rate:
                dropped += 1
                continue

            if jitter_ms > 0:
                await asyncio.sleep(random.uniform(0, jitter_ms) / 1000.0)

            capture_ts_ms = int((time.monotonic() - start) * 1000)
            frame = encode_frame(seq, capture_ts_ms, chunks[seq])
            await ws.send(frame)
            sent += 1

            # (Pacing happens at the top of the loop, against an absolute
            # schedule — see the comment there.)

        print(f"Done sending. Sent {sent} frames, simulated-dropped {dropped} frames.")

        # IMPORTANT: don't close the connection the instant the last frame
        # is sent. Deepgram (and, later, the rest of the pipeline) needs
        # time after the last bit of audio arrives to finish recognizing
        # any trailing speech and emit its final Results/UtteranceEnd
        # events. Closing immediately risks silently cutting off whatever
        # was still being processed — exactly the kind of gap that
        # produces a segment with audio but no transcript.
        if drain_seconds > 0:
            print(f"Waiting {drain_seconds:.1f}s to let the server finish processing trailing speech...")
            await asyncio.sleep(drain_seconds)

        listen_task.cancel()
        try:
            await listen_task
        except asyncio.CancelledError:
            pass

    print("Connection closed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audio", help="Path to a 16kHz mono PCM16 WAV file")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--session-id", default=None, help="Defaults to a random UUID")
    parser.add_argument("--frame-ms", type=int, default=FRAME_MS)
    parser.add_argument("--jitter-ms", type=int, default=0, help="Random extra delay per frame, 0-N ms")
    parser.add_argument("--drop-rate", type=float, default=0.0, help="Probability (0-1) of simulating a lost frame")
    parser.add_argument("--reorder-rate", type=float, default=0.0, help="Probability (0-1) of swapping adjacent frames' send order")
    parser.add_argument("--drain-seconds", type=float, default=2.0, help="Seconds to wait after the last frame before closing, so trailing speech can finish processing (default: 2.0)")
    parser.add_argument("--room", default=None, help="Room label (e.g. DEMO-101). Needed when the server runs with a database.")
    parser.add_argument("--token", default=None, help="Lecturer JWT (or set TEACHER_TOKEN). Needed with --room.")
    args = parser.parse_args()

    session_id = args.session_id or str(uuid.uuid4())
    if args.room:
        from urllib.parse import quote
        token = args.token or os.getenv("TEACHER_TOKEN", "")
        uri = f"ws://{args.host}:{args.port}/ws/teacher?room={quote(args.room)}&token={quote(token)}"
    else:
        uri = f"ws://{args.host}:{args.port}/ws/teacher?session_id={session_id}"

    asyncio.run(stream_audio(uri, args.audio, args.frame_ms, args.jitter_ms, args.drop_rate, args.reorder_rate, args.drain_seconds))


if __name__ == "__main__":
    main()
