"""Session Coordinator.

Wires together every piece built so far — TeacherAudioIngest ->
Segmenter -> TranslationQueue -> StudentPlaybackController — and now
also owns the Mode State Machine (Lecture <-> Question), implementing
the 13 product rules established early in this project:

  Rule 1 (single active speaker): enforced by ModeStateMachine itself —
    there is exactly one mode at any time.
  Rule 2 (instant, synchronous switch): press()/release() flip the mode
    immediately; nothing waits on anything else first.
  Rule 3 (in-flight teacher audio never discarded): achieved almost for
    free by this architecture — gating happens at *frame intake*
    (handle_teacher_frame), so anything already inside the jitter
    buffer / segmentation / translation queue keeps processing
    completely independently of mode. The one explicit action needed is
    force-closing the segment that was *in progress* at the moment of
    interruption (Segmenter.force_close()), so it doesn't sit waiting
    on a silence timeout that will never come once capture stops.
  Rule 4 (teacher not captured during Question Mode): enforced in
    handle_teacher_frame — frames are dropped before they ever reach
    TeacherAudioIngest.
  Rule 5 (playback pauses, not processing): the Translation Queue's
    workers have zero knowledge of mode and never stop. Only the
    /ws/student route's send loop pauses — see lecture_mode_event.
  Rule 6 (no segment dropped as "old"): unaffected by any of the above;
    TranslationQueue's FIFO guarantee (established before Question Mode
    existed) already covers this.
  Rule 7 / 8 (retry-then-skip vs retry-then-visible-failure): lecture
    segments already follow Rule 7 (translation_pipeline.py). Questions
    follow Rule 8 here — see _handle_question_result.
  Rule 11 (mic state visible to both): _broadcast_mode sends a
    mode_changed message to both connections on every transition.
  Rule 12 (debounce short presses): ModeStateMachine.release() reports
    this; a debounced press's captured audio is discarded without ever
    reaching the Question Pipeline.
  Rule 13 (mic stays live for the whole press, no silence cutoff):
    naturally true — frames are accepted for the question buffer for as
    long as mode stays Question, regardless of pauses in the student's
    speech. There's no VAD/silence logic on this path at all.

Rules 9 and 10 (no silence timeout needed; pre/post-roll buffering) are
primarily client-side concerns per the architecture spec (a local
circular buffer on the student's device) — see question_pipeline.py's
module docstring and the README for what this backend does and doesn't
implement of those two.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from .config import Config
from .mode_state import ModeStateMachine
from .playback import StudentPlaybackController
from .protocol import AudioFrame, decode_frame, ProtocolError
from .question_pipeline import process_question, QuestionResult, QuestionTranscriber
from .segmentation import Segmenter
from .teacher_ingest import TeacherAudioIngest
from .translation_pipeline import TranslatedSegment, TranslationQueue, write_debug_output

logger = logging.getLogger("session")

# Callbacks the server layer registers so the coordinator can push data
# out to whichever client needs it, without session.py importing
# anything about WebSockets directly.
SendToTeacher = Callable[[bytes], Awaitable[None]]
SendControlToStudent = Callable[[dict], Awaitable[None]]
SendControlToTeacher = Callable[[dict], Awaitable[None]]


class SessionCoordinator:
    """Single global session — one teacher, at most one student
    (multi-session / multi-student support is explicitly out of scope
    for the MVP; see architecture spec's MVP boundaries)."""

    def __init__(self, cfg: Config, transcriber_factory=None) -> None:
        self._cfg = cfg
        # Builds the streaming STT for each question; injectable so tests need no Deepgram.
        self._transcriber_factory = transcriber_factory or QuestionTranscriber
        self._transcriber = None
        self._bg_tasks: set = set()
        self._teacher_ingest: Optional[TeacherAudioIngest] = None
        self._segmenter: Optional[Segmenter] = None
        self._translation_queue: Optional[TranslationQueue] = None
        self._playback: Optional[StudentPlaybackController] = None
        self._teacher_session_id: Optional[str] = None

        self._mode = ModeStateMachine(min_press_duration_ms=cfg.question_min_duration_ms)
        self._question_audio = bytearray()

        # Rule 5: the /ws/student route's send loop waits on this rather
        # than polling. Set = Lecture Mode (safe to dequeue and send
        # translated audio); cleared = Question Mode (pause dequeuing).
        # Starts set, since sessions begin in Lecture Mode.
        self.lecture_mode_event = asyncio.Event()
        self.lecture_mode_event.set()

        # Registered by the server layer once each connection is live —
        # None until then, guarded before every use.
        self._send_audio_to_teacher: Optional[SendToTeacher] = None
        self._send_control_to_student: Optional[SendControlToStudent] = None
        self._send_control_to_teacher: Optional[SendControlToTeacher] = None

    # -----------------------------------------------------------------
    # Connection registration (called by server.py)
    # -----------------------------------------------------------------

    def register_teacher_sender(
        self, send_audio: SendToTeacher, send_control: SendControlToTeacher,
    ) -> None:
        self._send_audio_to_teacher = send_audio
        self._send_control_to_teacher = send_control

    def unregister_teacher_sender(self) -> None:
        self._send_audio_to_teacher = None
        self._send_control_to_teacher = None

    def register_student_sender(self, send_control: SendControlToStudent) -> None:
        self._send_control_to_student = send_control

    def unregister_student_sender(self) -> None:
        self._send_control_to_student = None

    @property
    def teacher_connected(self) -> bool:
        return self._teacher_ingest is not None

    @property
    def current_playback(self) -> Optional[StudentPlaybackController]:
        """Used by the /ws/student route to find the active session's
        playback controller. Returns None if no teacher session is
        currently active — the student route waits for one."""
        return self._playback

    @property
    def is_question_mode(self) -> bool:
        return self._mode.is_question_mode

    @property
    def has_student(self) -> bool:
        """Only one student connection per session is supported so far: translated
        audio goes to a single playback queue, so a second student would silently
        steal every other segment. The server rejects a second one instead."""
        return self._send_control_to_student is not None

    @property
    def current_mode(self) -> str:
        """'lecture' or 'question'. Sent to each client the moment it
        connects, so a client that joins mid-session (or while a
        question is in progress) doesn't have to wait for the next
        transition to learn what mode it's in (Rule 11)."""
        return self._mode.mode

    # -----------------------------------------------------------------
    # Teacher lifecycle (unchanged from the pre-Question-Mode version,
    # aside from passing through to the same place either way)
    # -----------------------------------------------------------------

    async def attach_teacher(self, session_id: str) -> None:
        if self._teacher_ingest is not None:
            logger.warning(
                "A teacher session (%s) is already active; replacing it with %s. "
                "Multi-session support is explicitly out of scope for the MVP.",
                self._teacher_session_id, session_id,
            )
            await self._detach_current()

        playback = StudentPlaybackController(session_id)

        def on_translated(result: TranslatedSegment) -> None:
            # Composed, not swapped: keep writing debug files for manual
            # inspection *and* feed the real playback reorder buffer, so
            # this stays verifiable even while a real student is connected.
            write_debug_output(self._cfg, result)
            playback.submit(result)

        translation_queue = TranslationQueue(self._cfg, session_id, on_translated=on_translated)
        translation_queue.start()

        segmenter = Segmenter(self._cfg, session_id, on_segment=translation_queue.enqueue)
        await segmenter.start()

        ingest = TeacherAudioIngest(self._cfg, session_id, on_smoothed_frame=segmenter.handle_frame)
        await ingest.start()

        self._playback = playback
        self._translation_queue = translation_queue
        self._segmenter = segmenter
        self._teacher_ingest = ingest
        self._teacher_session_id = session_id
        logger.info(
            "Teacher session %s attached (ingest -> segmentation -> translation -> playback wired)",
            session_id,
        )

    async def detach_teacher(self, session_id: str) -> None:
        if self._teacher_session_id == session_id:
            await self._detach_current()
            logger.info("Teacher session %s detached", session_id)

    async def _detach_current(self) -> None:
        # Order matters, each step depends on the previous one having
        # fully stopped: stop ingest first (no new frames arrive
        # mid-shutdown), then the segmenter (flushes its final partial
        # segment, which may still enqueue one more item), then the
        # translation queue (drains whatever segmentation just handed
        # it, submitting final results to playback along the way).
        if self._teacher_ingest is not None:
            await self._teacher_ingest.stop()
            self._teacher_ingest = None
        if self._segmenter is not None:
            await self._segmenter.stop()
            self._segmenter = None
        if self._translation_queue is not None:
            await self._translation_queue.stop()
            self._translation_queue = None
        self._playback = None
        self._teacher_session_id = None

    def handle_teacher_frame(self, session_id: str, raw: bytes) -> None:
        # Rule 4: teacher speech is not captured at all during Question
        # Mode. Gating it here — before the frame ever reaches
        # TeacherAudioIngest — is what makes that true; nothing
        # downstream needs to know mode exists.
        if self._mode.is_question_mode:
            return
        if self._teacher_ingest is not None and self._teacher_session_id == session_id:
            self._teacher_ingest.handle_raw_message(raw)

    # -----------------------------------------------------------------
    # Question Mode
    # -----------------------------------------------------------------

    async def handle_ask_press(self) -> None:
        if self._mode.is_question_mode:
            logger.warning("Received ask_press while already in Question Mode — ignoring duplicate press")
            return

        self._mode.press()
        self._question_audio = bytearray()
        self.lecture_mode_event.clear()

        # Start streaming this question to STT now, while the student is still
        # speaking. Opening the Deepgram connection takes several seconds, and
        # doing it at release (as before) put all of that after they finished.
        self._transcriber = self._transcriber_factory(self._cfg, self._teacher_session_id or "no-active-teacher")
        self._transcriber.start()

        # Rule 3: the segment that was mid-flight when the teacher got
        # interrupted finishes immediately rather than waiting on a
        # silence timeout that will never fire now that capture has
        # stopped (see handle_teacher_frame's gating above).
        if self._segmenter is not None:
            await self._segmenter.force_close()

        logger.info("Question Mode entered")
        await self._broadcast_mode()

    async def handle_ask_release(self) -> None:
        if not self._mode.is_question_mode:
            logger.warning("Received ask_release while not in Question Mode — ignoring stray release")
            return

        press_result = self._mode.release()
        captured_audio = bytes(self._question_audio)
        self._question_audio = bytearray()
        self.lecture_mode_event.set()
        transcriber, self._transcriber = self._transcriber, None

        logger.info("Question Mode exited (held %.0fms)", press_result.held_ms)
        await self._broadcast_mode()

        if not press_result.is_real_press:
            self._abort_in_background(transcriber)
            # Rule 12: too short to be a real question — discarded
            # silently. The mode switch above still happened normally;
            # only the captured audio is thrown away.
            logger.info("Press was only %.0fms (< %dms threshold) — treated as accidental, discarding",
                        press_result.held_ms, self._cfg.question_min_duration_ms)
            return

        if not captured_audio:
            self._abort_in_background(transcriber)
            logger.info("No audio was captured during the press — nothing to process")
            return

        # Deliberately not awaited inline: the question is processed in
        # the background so handle_ask_release (and the mode switch
        # above) returns immediately. The teacher resumes instantly;
        # the question's translation catches up whenever it's ready.
        asyncio.create_task(self._process_and_deliver_question(captured_audio, transcriber))

    async def handle_student_disconnect(self) -> None:
        """Called by the server when the student's connection ends.

        If the student was holding Ask when the connection dropped,
        nothing will ever send ask_release, so without this the session
        would stay in Question Mode forever: teacher audio gated
        (Rule 4) and lecture playback paused (Rule 5), with nobody able
        to end it. This reverts to Lecture Mode and tells the teacher
        (Rule 11).

        The partial question is discarded, not processed. It was cut
        off by the drop, so translating it would speak a half-finished
        sentence to the teacher, and the student is gone, so they can't
        be told whether it was delivered or failed (Rule 8). If you'd
        rather deliver what was captured, call handle_ask_release()
        here instead of discarding.

        A no-op when the student wasn't mid-question, which is the
        normal case.
        """
        if not self._mode.is_question_mode:
            return

        press_result = self._mode.release()
        discarded_bytes = len(self._question_audio)
        transcriber, self._transcriber = self._transcriber, None
        self._abort_in_background(transcriber)
        self._question_audio = bytearray()
        self.lecture_mode_event.set()
        logger.warning(
            "Student disconnected mid-question (held %.0fms, %d bytes of question audio "
            "discarded) - reverting to Lecture Mode",
            press_result.held_ms, discarded_bytes,
        )
        await self._broadcast_mode()

    def handle_question_audio_frame(self, raw: bytes) -> None:
        """Called by the /ws/student route for binary messages arriving
        while in Question Mode. Rule 13: accepted for the entire
        duration the button is held, with no silence-based cutoff."""
        if not self._mode.is_question_mode:
            logger.debug("Ignoring question audio frame received outside Question Mode")
            return
        try:
            frame: AudioFrame = decode_frame(raw)
        except ProtocolError as e:
            logger.warning("Dropping malformed question audio frame: %s", e)
            return
        self._question_audio.extend(frame.payload)
        if self._transcriber is not None:
            self._transcriber.feed(frame.payload)

    def _abort_in_background(self, transcriber) -> None:
        """Closes a question's streaming STT without making the caller wait
        on the socket teardown (the mode switch must stay instant)."""
        if transcriber is None:
            return

        async def _abort() -> None:
            try:
                await transcriber.abort()
            except Exception:  # noqa: BLE001
                logger.debug("Aborting question transcriber failed", exc_info=True)

        task = asyncio.create_task(_abort())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _process_and_deliver_question(self, audio: bytes, transcriber=None) -> None:
        session_id = self._teacher_session_id or "no-active-teacher"
        result: QuestionResult = await process_question(self._cfg, session_id, audio, transcriber=transcriber)
        await self._handle_question_result(result)

    async def _handle_question_result(self, result: QuestionResult) -> None:
        if result.success:
            logger.info(
                "Question delivered: %r -> %r (%d bytes)",
                result.source_transcript, result.translated_text, len(result.audio or b""),
            )
            if self._send_audio_to_teacher is not None:
                await self._send_audio_to_teacher(result.audio)
            if self._send_control_to_student is not None:
                await self._send_control_to_student({
                    "type": "question_delivered",
                    "source_transcript": result.source_transcript,
                    "translated_text": result.translated_text,
                })
        else:
            # Rule 8: unlike a failed lecture segment (which fails
            # silently per Rule 7), a failed question must be visible —
            # the student needs to know to try again, since the whole
            # interaction is otherwise just gone.
            logger.info("Question failed: %s", result.error)
            if self._send_control_to_student is not None:
                await self._send_control_to_student({
                    "type": "question_failed",
                    "error": result.error,
                    "source_transcript": result.source_transcript,
                })

    async def _broadcast_mode(self) -> None:
        # Rule 11: mic state visible to both users. Guarded — either
        # side may not be connected yet/anymore, and that's fine, the
        # other side still gets it.
        message = {"type": "mode_changed", "mode": self._mode.mode}
        if self._send_control_to_student is not None:
            await self._send_control_to_student(message)
        if self._send_control_to_teacher is not None:
            await self._send_control_to_teacher(message)
