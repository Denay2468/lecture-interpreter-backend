"""Server, transport, and jitter buffer configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


@dataclass
class Config:
    # Realtime Transport
    host: str = "0.0.0.0"
    port: int = 8765

    # Audio format — matches the benchmark project for consistency.
    sample_rate_hz: int = 16000
    bytes_per_sample: int = 2  # 16-bit PCM
    channels: int = 1

    # Frame size the teacher client is expected to send. The architecture
    # spec targets 20-50ms frames to keep per-frame latency low; this is
    # informational for the client side, the server accepts whatever
    # frame size arrives and doesn't enforce it.
    target_frame_ms: int = 20

    # --- Jitter Buffer ---
    # How long a frame sits in the buffer before being released, to
    # absorb ordinary network jitter/burstiness. Tune this once you have
    # real network measurements (see the "test early" note in the
    # architecture spec's transport risk).
    jitter_target_delay_ms: int = 150
    # If a frame is still missing (out-of-sequence gap) after this long,
    # stop waiting for it and move on — a jitter buffer that waits
    # forever for one lost packet defeats its own purpose, and would
    # violate the "processing never blocks" principle from the spec.
    jitter_max_wait_ms: int = 400
    # How often the release loop checks whether anything is ready to
    # flush. Smaller = more precise timing, more CPU wakeups.
    jitter_tick_ms: int = 10

    # --- Debug/verification output ---
    # This vertical slice doesn't have the Translation Pipeline/Queue or
    # Student Playback wired up yet, so Segmentation's default downstream
    # consumer writes each finalized segment to its own WAV file (plus
    # its transcript) so you can listen to and read exactly what the
    # segmentation logic decided to cut, and where.
    debug_output_dir: str = "debug_output"

    # --- Segmentation (hybrid STT-completion + VAD) ---
    # Reuses the same Deepgram account/model as the benchmark project.
    deepgram_api_key: Optional[str] = None
    deepgram_model: str = "nova-2"
    segmentation_language: str = "id"  # Indonesian (teacher's language)

    # Primary trigger: Deepgram's own endpointing, in ms. When a pause of
    # at least this long follows speech, Deepgram marks that Results
    # message `speech_final=True` — this is closer to a real "utterance
    # complete" signal than raw silence, since it's endpointing-informed.
    #
    # Empirically tuned, not guessed: 300ms was the original starting
    # point, but real comparative testing on conversational Indonesian
    # speech showed it caused speech_final to fire on only ~1 in 7-8
    # finalized chunks — most natural pauses between clauses were
    # shorter than 300ms, so segments mostly fell back to the slower
    # UtteranceEnd trigger or the max-duration cap instead. 50ms raised
    # the speech_final hit rate to ~6 in 7 on the same test material,
    # with no observed mid-sentence fragmentation. Revisit if later
    # testing (different speakers, more natural pacing, background
    # noise) shows 50ms cutting sentences too early.
    segmentation_endpointing_ms: int = 50
    # Fallback/backstop trigger: Deepgram's UtteranceEnd event, a
    # separate and longer silence timer that exists specifically to
    # catch cases where endpointing doesn't fire (e.g. a chunk got cut
    # off oddly). Must be >= 1000ms per Deepgram's own constraints.
    segmentation_utterance_end_ms: int = 1000
    # Hard cap, not part of the architecture spec's original two
    # triggers — an added safety net so one segment can never grow
    # unboundedly if neither trigger fires (e.g. unusually shaped
    # continuous speech). Forces a segment closed regardless.
    segmentation_max_duration_ms: int = 15000

    # --- Translation Pipeline (MT -> TTS per segment) ---
    # Reuses the same accounts/providers validated in the benchmark
    # project. STT already happened during Segmentation, so this stage
    # is just MT -> TTS.
    deepl_api_key: Optional[str] = None
    deepl_api_url: str = "https://api-free.deepl.com/v2/translate"
    elevenlabs_api_key: Optional[str] = None
    elevenlabs_voice_id: Optional[str] = None

    # How many segments can be translated/synthesized concurrently.
    # Architecture spec Section 4: processing must never block on mode
    # state, but concurrency here is still bounded so a fast-talking
    # teacher can't spawn unlimited simultaneous API calls.
    translation_num_workers: int = 2
    # Architecture spec Section 7 / Rule 7: one automatic retry, then
    # skip and continue — no user-facing error, no indefinite blocking.
    translation_max_attempts: int = 2
    # How long stop() waits for already-enqueued segments to finish
    # processing before giving up and cancelling — matches the
    # "never silently discard in-flight work" pattern already used by
    # the Jitter Buffer's flush() and Teacher Audio Ingest's drain.
    translation_drain_timeout_s: float = 30.0
    # Shared budget across both stages and retries, once a worker starts.
    translation_segment_timeout_s: float = 30.0
    # Includes WebSocket connect, reads and normal teardown, not each recv.
    tts_total_timeout_s: float = 20.0
    debug_audio_enabled: bool = False

    # --- Database / multi-tenant mode ---
    # Unset DATABASE_URL = single-session mode: no accounts, rooms or metering
    # (what the local test scripts use). Set it and lectures are recorded.
    database_url: Optional[str] = None
    auto_migrate: bool = True
    jwt_secret: Optional[str] = None
    heartbeat_interval_s: float = 30.0
    # A LIVE session whose heartbeat is older than this is treated as crashed and aborted.
    stale_session_after_s: float = 90.0

    # --- HTTP API and accounts (multi-tenant mode) ---
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = "http://localhost:8080,http://127.0.0.1:8080"  # comma-separated pages allowed to call the API
    access_token_ttl_s: int = 900            # 15 minutes
    refresh_token_ttl_s: int = 7 * 24 * 3600
    email_code_ttl_s: int = 900
    email_backend: str = "console"           # "console" prints codes in the server log (development); "smtp" really sends
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from: Optional[str] = None

    # --- Question Mode (Student -> Teacher direction) ---
    # Rule 12: a press held for less than this is treated as accidental
    # and silently ignored — no question sent, no teacher interruption
    # wasted on noise.
    question_min_duration_ms: int = 300
    # The student's premade voice (ELEVENLABS_VOICE_ID above) is an
    # English voice. For the question's Indonesian TTS output, ideally
    # a separate Indonesian-suited voice is configured here; if left
    # unset, the pipeline falls back to reusing the same voice ID —
    # ElevenLabs' multilingual model handles this reasonably, but this
    # is a known compromise worth revisiting once you can test
    # Indonesian pronunciation quality directly.
    elevenlabs_voice_id_question: Optional[str] = None


def load_config(env_path: str | None = None) -> Config:
    if env_path:
        load_dotenv(env_path)
    else:
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    def _get(key: str, default):
        val = os.getenv(key)
        return type(default)(val) if val not in (None, "") else default

    cfg = Config()
    cfg.host = os.getenv("HOST", cfg.host)
    cfg.port = _get("PORT", cfg.port)
    cfg.sample_rate_hz = _get("SAMPLE_RATE_HZ", cfg.sample_rate_hz)
    cfg.target_frame_ms = _get("TARGET_FRAME_MS", cfg.target_frame_ms)
    cfg.jitter_target_delay_ms = _get("JITTER_TARGET_DELAY_MS", cfg.jitter_target_delay_ms)
    cfg.jitter_max_wait_ms = _get("JITTER_MAX_WAIT_MS", cfg.jitter_max_wait_ms)
    cfg.jitter_tick_ms = _get("JITTER_TICK_MS", cfg.jitter_tick_ms)
    cfg.debug_output_dir = os.getenv("DEBUG_OUTPUT_DIR", cfg.debug_output_dir)

    cfg.deepgram_api_key = os.getenv("DEEPGRAM_API_KEY", cfg.deepgram_api_key)
    cfg.deepgram_model = os.getenv("DEEPGRAM_MODEL", cfg.deepgram_model)
    cfg.segmentation_language = os.getenv("SEGMENTATION_LANGUAGE", cfg.segmentation_language)
    cfg.segmentation_endpointing_ms = _get("SEGMENTATION_ENDPOINTING_MS", cfg.segmentation_endpointing_ms)
    cfg.segmentation_utterance_end_ms = _get("SEGMENTATION_UTTERANCE_END_MS", cfg.segmentation_utterance_end_ms)
    cfg.segmentation_max_duration_ms = _get("SEGMENTATION_MAX_DURATION_MS", cfg.segmentation_max_duration_ms)

    cfg.deepl_api_key = os.getenv("DEEPL_API_KEY", cfg.deepl_api_key)
    cfg.deepl_api_url = os.getenv("DEEPL_API_URL", cfg.deepl_api_url)
    cfg.elevenlabs_api_key = os.getenv("ELEVENLABS_API_KEY", cfg.elevenlabs_api_key)
    cfg.elevenlabs_voice_id = os.getenv("ELEVENLABS_VOICE_ID", cfg.elevenlabs_voice_id)
    cfg.translation_num_workers = _get("TRANSLATION_NUM_WORKERS", cfg.translation_num_workers)
    cfg.translation_max_attempts = _get("TRANSLATION_MAX_ATTEMPTS", cfg.translation_max_attempts)
    cfg.translation_drain_timeout_s = _get("TRANSLATION_DRAIN_TIMEOUT_S", cfg.translation_drain_timeout_s)
    cfg.translation_segment_timeout_s = _get("TRANSLATION_SEGMENT_TIMEOUT_S", cfg.translation_segment_timeout_s)
    cfg.tts_total_timeout_s = _get("TTS_TOTAL_TIMEOUT_S", cfg.tts_total_timeout_s)
    cfg.debug_audio_enabled = os.getenv("DEBUG_AUDIO_ENABLED", "false").strip().lower() in ("1", "true", "yes")
    if cfg.translation_segment_timeout_s <= 0 or cfg.tts_total_timeout_s <= 0:
        raise ValueError("Translation and TTS timeouts must be positive")
    if cfg.translation_num_workers < 1 or cfg.translation_max_attempts < 1:
        raise ValueError("Translation workers and attempts must be at least one")

    cfg.database_url = os.getenv("DATABASE_URL") or cfg.database_url
    cfg.jwt_secret = os.getenv("JWT_SECRET") or cfg.jwt_secret
    cfg.auto_migrate = os.getenv("AUTO_MIGRATE", "true").strip().lower() in ("1", "true", "yes")
    cfg.heartbeat_interval_s = _get("HEARTBEAT_INTERVAL_S", cfg.heartbeat_interval_s)
    cfg.stale_session_after_s = _get("STALE_SESSION_AFTER_S", cfg.stale_session_after_s)
    cfg.api_host = os.getenv("API_HOST") or cfg.api_host
    cfg.api_port = _get("API_PORT", cfg.api_port)
    cfg.cors_origins = os.getenv("CORS_ORIGINS") or cfg.cors_origins
    cfg.access_token_ttl_s = _get("ACCESS_TOKEN_TTL_S", cfg.access_token_ttl_s)
    cfg.refresh_token_ttl_s = _get("REFRESH_TOKEN_TTL_S", cfg.refresh_token_ttl_s)
    cfg.email_code_ttl_s = _get("EMAIL_CODE_TTL_S", cfg.email_code_ttl_s)
    cfg.email_backend = (os.getenv("EMAIL_BACKEND") or cfg.email_backend).strip().lower()
    cfg.smtp_host = os.getenv("SMTP_HOST") or cfg.smtp_host
    cfg.smtp_port = _get("SMTP_PORT", cfg.smtp_port)
    cfg.smtp_user = os.getenv("SMTP_USER") or cfg.smtp_user
    cfg.smtp_password = os.getenv("SMTP_PASSWORD") or cfg.smtp_password
    cfg.smtp_from = os.getenv("SMTP_FROM") or cfg.smtp_from

    cfg.question_min_duration_ms = _get("QUESTION_MIN_DURATION_MS", cfg.question_min_duration_ms)
    cfg.elevenlabs_voice_id_question = os.getenv("ELEVENLABS_VOICE_ID_QUESTION", cfg.elevenlabs_voice_id_question)
    return cfg
