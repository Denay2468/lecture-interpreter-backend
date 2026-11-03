"""Ordered question phrases delivered while the student holds Ask.

Uses the existing teacher MP3 player; each phrase is synthesized separately.
No full-question replay after partial delivery (would duplicate spoken words).
"""
import asyncio
import time
from .question_pipeline import QuestionTranscriber


class LiveQuestionTranscriber(QuestionTranscriber):
    def __init__(self, cfg, session_id, translate, synthesize, deliver, **kwargs):
        super().__init__(cfg, session_id, **kwargs)
        self._translate = translate
        self._synthesize = synthesize
        self._deliver = deliver
        self._phrases = asyncio.Queue(maxsize=32)
        self._worker = None
        self._phrase_error = None
        self._translations = []

    def start(self):
        super().start()
        self._worker = asyncio.create_task(self._run_phrases())

    def _on_event(self, kind, text):
        super()._on_event(kind, text)
        if kind == 'final' and text and not self._closed:
            try:
                self._phrases.put_nowait(text)
            except asyncio.QueueFull:
                self._phrase_error = 'Question translation queue is full; please ask again.'

    async def _run_phrases(self):
        while True:
            text = await self._phrases.get()
            try:
                if text is None:
                    return
                if self._phrase_error:
                    continue
                # Preserve accidental-press behavior before any delivery.
                await asyncio.sleep(max(0, self._cfg.question_min_duration_ms / 1000
                                        - (time.monotonic() - self._t_start)))
                async with asyncio.timeout(self._cfg.translation_segment_timeout_s):
                    translated = await self._translate(text)
                    audio = await self._synthesize(translated)
                    await self._deliver(audio)
                    self._translations.append(translated)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._phrase_error = 'Question phrase processing failed; please ask again.'
            finally:
                self._phrases.task_done()

    async def finish_live(self):
        try:
            source = await super().finish()
            await asyncio.wait_for(self._phrases.join(), self._cfg.translation_drain_timeout_s)
            if self._phrase_error:
                raise RuntimeError(self._phrase_error)
            return source, ' '.join(self._translations)
        finally:
            await self.abort()

    async def abort(self):
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        await super().abort()
