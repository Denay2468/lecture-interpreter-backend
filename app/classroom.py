"""One playback consumer per room, bounded independent delivery per student."""
import asyncio
import json
import logging
import time
import uuid
from collections import deque
from .protocol import encode_playback_frame
from .playback import StreamingSegment
from .telemetry import percentile, estimate_cost

log = logging.getLogger("classroom")


class Classroom:
    def __init__(self, coordinator):
        self.coord = coordinator
        self.students = {}
        self.waiting = []
        self.owner = None
        self.owner_since = 0
        self.teacher_send = None
        self.tasks = []
        self.played_ms = deque(maxlen=500)
        self.started = time.monotonic()

    def start(self):
        if self.tasks:
            return
        self.coord.register_student_sender(self.control)
        self.tasks = [asyncio.create_task(self.pump()), asyncio.create_task(self.monitor())]

    def put(self, student, message):
        entry = self.students.get(student)
        if not entry:
            return
        try:
            entry["queue"].put_nowait(message)
        except asyncio.QueueFull:
            # A slow browser must not delay the room or accumulate stale audio.
            entry["overflow"].set()

    async def control(self, message):
        if message.get("type") in ("question_delivered", "question_failed"):
            self.put(self.owner, message)
        else:
            for student in list(self.students):
                self.put(student, message)

    async def announce(self):
        data = {"type": "classroom", "students": len(self.students),
                "waiting": list(self.waiting), "speaker": self.owner}
        await self.control(data)
        if self.teacher_send:
            await self.teacher_send(data)

    async def join(self, websocket):
        student = uuid.uuid4().hex[:12]
        self.students[student] = {"ws": websocket, "queue": asyncio.Queue(maxsize=32),
                                  "overflow": asyncio.Event(), "sent": {}}
        self.start()
        self.put(student, {"type": "welcome", "student_id": student})
        self.put(student, {"type": "mode_changed", "mode": self.coord.current_mode})
        await self.announce()
        return student

    async def leave(self, student):
        self.students.pop(student, None)
        if student in self.waiting:
            self.waiting.remove(student)
        if self.owner == student:
            await self.coord.handle_student_disconnect()
            # Keep the owner reserved while already-released audio is processed.
            if not self.coord.question_busy:
                self.owner = None
        await self.announce()

    async def request(self, student):
        if student in self.students and student != self.owner and student not in self.waiting:
            self.waiting.append(student)
            await self.announce()

    async def grant(self, student):
        if self.owner or self.coord.question_busy or student not in self.waiting:
            return
        self.waiting.remove(student)
        self.owner = student
        self.owner_since = time.monotonic()
        self.put(student, {"type": "question_granted"})
        await self.announce()

    async def receive(self, student, message):
        if isinstance(message, bytes):
            if student == self.owner:
                self.coord.handle_question_audio_frame(message)
            return
        try:
            data = json.loads(message)
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        kind = data.get("type")
        if kind == "request_question":
            await self.request(student)
        elif kind == "cancel_question":
            if student in self.waiting:
                self.waiting.remove(student)
            if student == self.owner and not self.coord.question_busy:
                self.owner = None
            await self.announce()
        elif kind == "ask_press" and student == self.owner and self.owner_since > 0 and not self.coord.question_busy:
            self.owner_since = time.monotonic()
            await self.coord.handle_ask_press()
        elif kind == "ask_release" and student == self.owner:
            await self.coord.handle_ask_release()
            self.owner_since = 0
            if not self.coord.question_busy:
                self.owner = None
                await self.announce()
        elif kind == "playback_started":
            seq = data.get("sequence")
            if isinstance(seq, int):
                stamp = self.students[student]["sent"].pop(seq, None)
                if stamp:
                    elapsed = (time.monotonic() - stamp) * 1000
                    self.played_ms.append(elapsed)
                    log.info("latency stage=segment_ready_to_playback_ack sequence=%d elapsed_ms=%.1f", seq, elapsed)

    async def pump(self):
        while True:
            controller = self.coord.current_playback
            if controller is None:
                await asyncio.sleep(.1)
                continue
            result = await controller.get_next()
            await self.coord.lecture_mode_event.wait()
            if isinstance(result, StreamingSegment):
                recipients = list(self.students)
                while True:
                    try:
                        event = await asyncio.wait_for(result.events.get(), 35)
                    except asyncio.TimeoutError:
                        event = {"type": "segment_end", "sequence": result.sequence, "audio_failed": True}
                    await self.coord.lecture_mode_event.wait()
                    for student in recipients:
                        entry = self.students.get(student)
                        if entry is None:
                            continue
                        if event["type"] == "segment":
                            entry["sent"][result.sequence] = result.segment_ready_at
                            while len(entry["sent"]) > 100:
                                entry["sent"].pop(next(iter(entry["sent"])))
                        self.put(student, event)
                    if event["type"] == "segment_end":
                        break
                continue
            message = {"type": "segment", "sequence": result.sequence,
                       "source_transcript": result.source_transcript,
                       "translated_text": result.translated_text,
                       "audio_available": bool(result.audio)}
            for student, entry in list(self.students.items()):
                if result.audio:
                    entry["sent"][result.sequence] = result.segment_ready_at or time.monotonic()
                    while len(entry["sent"]) > 100:
                        entry["sent"].pop(next(iter(entry["sent"])))
                # Text and audio are one queue item: never separate their order.
                self.put(student, (message, encode_playback_frame(result.sequence, result.audio) if result.audio else None))

    async def send_loop(self, student):
        entry = self.students[student]
        while True:
            message = await entry["queue"].get()
            try:
                if isinstance(message, tuple):
                    await entry["ws"].send(json.dumps(message[0]))
                    if message[1]:
                        await entry["ws"].send(message[1])
                else:
                    await entry["ws"].send(json.dumps(message))
            finally:
                entry["queue"].task_done()

    async def monitor(self):
        while True:
            await asyncio.sleep(1)
            # A grant expires after 45s, including a stuck microphone press.
            if self.owner and self.owner_since > 0 and time.monotonic() - self.owner_since > 45 and not self.coord.question_processing:
                await self.coord.handle_student_disconnect()
                self.put(self.owner, {"type": "question_expired"})
                self.owner = None
                await self.announce()
            # Delivery completion releases the speaker even if they disconnected.
            if self.owner and not self.coord.question_busy and self.owner_since == 0:
                self.owner = None
                await self.announce()
            if self.teacher_send:
                metrics = self.coord.metrics()
                ordered = sorted(self.played_ms)
                metrics.update(type="metrics", playback_samples=len(ordered),
                               segment_to_playback_ack_p50_ms=percentile(ordered, 50),
                               segment_to_playback_ack_p95_ms=percentile(ordered, 95),
                               elapsed_s=time.monotonic()-self.started)
                elapsed = metrics["elapsed_s"]
                cfg = self.coord._cfg
                rates = (cfg.cost_stt_per_minute, cfg.cost_mt_per_million, cfg.cost_tts_per_million)
                estimate = estimate_cost(elapsed, metrics.get("mt_characters", 0), metrics.get("tts_characters", 0), rates)
                metrics.update(estimated_cost=estimate, cost_currency=cfg.cost_currency,
                               projected_hourly_cost=estimate*3600/max(elapsed, 1) if estimate is not None else None)
                await self.teacher_send(metrics)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        self.coord.unregister_student_sender()
        await asyncio.gather(*(e["ws"].close(code=4413, reason="The lecture has ended") for e in self.students.values()), return_exceptions=True)
