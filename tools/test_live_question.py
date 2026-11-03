import asyncio
import unittest
from unittest.mock import AsyncMock
from app.config import Config
from app.live_question import LiveQuestionTranscriber

class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivers_before_release_in_order_and_abort_stops_worker(self):
        out=[]
        async def send(audio): out.append(audio)
        cfg=Config(); cfg.question_min_duration_ms=0
        t=LiveQuestionTranscriber(cfg,'s',AsyncMock(side_effect=lambda x:x.upper()),
                                  AsyncMock(side_effect=lambda x:x.encode()),send)
        t._worker=asyncio.create_task(t._run_phrases())
        t._on_event('final','hello'); t._on_event('final','world')
        await t._phrases.join()
        self.assertEqual(out,[b'HELLO',b'WORLD'])
        self.assertFalse(t._finishing)
        await t.abort()
        self.assertIsNone(t._worker)
    async def test_failure_does_not_replay_or_send_later_phrases(self):
        send=AsyncMock()
        cfg=Config(); cfg.question_min_duration_ms=0
        t=LiveQuestionTranscriber(cfg,'s',AsyncMock(side_effect=RuntimeError('secret')),AsyncMock(),send)
        t._worker=asyncio.create_task(t._run_phrases())
        t._on_event('final','hello'); t._on_event('final','world')
        await t._phrases.join()
        send.assert_not_awaited()
        self.assertNotIn('secret',t._phrase_error)
        await t.abort()
