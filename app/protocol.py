"""MVP wire protocol for teacher audio frames.

Each binary WebSocket message the teacher client sends is:

    [ 4 bytes: sequence number, uint32 big-endian ]
    [ 4 bytes: capture timestamp (ms since client started), uint32 big-endian ]
    [ N bytes: raw PCM16LE audio payload ]

The sequence number and capture timestamp are what let the Jitter
Buffer detect out-of-order arrival and reorder frames correctly — this
is the whole reason the protocol needs a header at all, rather than
just streaming raw PCM bytes.

NOTE: this is a placeholder protocol for this vertical slice. A real
browser/mobile teacher client will need to implement this exact framing
(or the protocol gets revisited together when that client is built —
whichever comes first). It is intentionally simple: no compression, no
Opus, no WebRTC — matching the "start simple, test early, upgrade the
transport only if real jitter/packet-loss testing shows it's needed"
decision from the architecture spec.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

_HEADER_FORMAT = ">II"  # 2x uint32, big-endian
HEADER_SIZE = struct.calcsize(_HEADER_FORMAT)


@dataclass(frozen=True)
class AudioFrame:
    sequence: int
    capture_timestamp_ms: int
    payload: bytes


class ProtocolError(ValueError):
    """Raised when an incoming WebSocket message doesn't match the
    expected frame format (too short, malformed header, etc)."""


def encode_frame(sequence: int, capture_timestamp_ms: int, payload: bytes) -> bytes:
    """Used by the test client (and, later, real teacher clients) to
    build an outgoing frame."""
    header = struct.pack(_HEADER_FORMAT, sequence & 0xFFFFFFFF, capture_timestamp_ms & 0xFFFFFFFF)
    return header + payload


def decode_frame(raw: bytes) -> AudioFrame:
    """Used by the server to parse an incoming WebSocket binary message."""
    if len(raw) < HEADER_SIZE:
        raise ProtocolError(f"Frame too short: got {len(raw)} bytes, need at least {HEADER_SIZE}")
    sequence, capture_timestamp_ms = struct.unpack(_HEADER_FORMAT, raw[:HEADER_SIZE])
    payload = raw[HEADER_SIZE:]
    return AudioFrame(sequence=sequence, capture_timestamp_ms=capture_timestamp_ms, payload=payload)


# ---------------------------------------------------------------------
# Server -> student direction. Simpler than the teacher direction: each
# translated segment is already a complete, self-contained synthesized
# clip (not a continuous real-time stream needing frame-level pacing),
# so one WebSocket binary message = one full segment's audio. The only
# header needed is the sequence number, so a test/real student client
# can confirm segments actually arrived in the correct order (Ordered
# Student Playback already guarantees this server-side — this header
# just makes that guarantee independently checkable on the client too).
# ---------------------------------------------------------------------

_PLAYBACK_HEADER_FORMAT = ">I"  # 1x uint32, big-endian
PLAYBACK_HEADER_SIZE = struct.calcsize(_PLAYBACK_HEADER_FORMAT)


def encode_playback_frame(sequence: int, audio: bytes) -> bytes:
    return struct.pack(_PLAYBACK_HEADER_FORMAT, sequence & 0xFFFFFFFF) + audio


def decode_playback_frame(raw: bytes) -> tuple[int, bytes]:
    if len(raw) < PLAYBACK_HEADER_SIZE:
        raise ProtocolError(f"Playback frame too short: got {len(raw)} bytes, need at least {PLAYBACK_HEADER_SIZE}")
    (sequence,) = struct.unpack(_PLAYBACK_HEADER_FORMAT, raw[:PLAYBACK_HEADER_SIZE])
    return sequence, raw[PLAYBACK_HEADER_SIZE:]
