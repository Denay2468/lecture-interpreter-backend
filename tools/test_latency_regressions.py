"""Offline latency/lifecycle regressions: python -m unittest tools.test_latency_regressions."""
import asyncio
import threading
import unittest
from unittest.mock import patch

from app.config import Config
from app.segmentation import Segment
from app.session import SessionCoordinator
from app.translation_pipeline import TranslationHTTPClient, TranslationQueue, synthesize_speech


def segment(seq):
    return Segment('test', seq, b'00', str(seq), 'speech_final', 20)


class LatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_pool_reused_for_both_directions_and_closed(self):
        sessions = []
        class Response:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def json(self): return {'translations': [{'text': 'translated'}]}
        class Session:
            def __init__(self):
                self.closed = False
                self.requests = []
                sessions.append(self)
            def post(self, url, **kwargs):
                self.requests.append(kwargs['data'])
                return Response()
            async def close(self): self.closed = True
        with patch('app.translation_pipeline.aiohttp.ClientSession', Session):
            client = TranslationHTTPClient(Config(deepl_api_key='test'))
            await asyncio.gather(client.translate('hello'), client.translate('question', 'EN', 'ID'))
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].requests[1]['target_lang'], 'ID')
            await client.close()
            self.assertTrue(sessions[0].closed)
            with self.assertRaises(RuntimeError): await client.translate('late')

    async def test_stuck_segment_advances_order_and_next_segment_runs(self):
        results = []
        calls = []
        async def translate(text):
            calls.append(text)
            if text == '0': await asyncio.Event().wait()
            return text
        async def tts(text): return b'audio'
        q = TranslationQueue(Config(translation_num_workers=1, translation_segment_timeout_s=.03),
                             'test', results.append, translate, tts)
        q.start()
        await q.enqueue(segment(0))
        await q.enqueue(segment(1))
        await asyncio.wait_for(q.stop(), 1)
        self.assertEqual([(r.sequence, r.state) for r in results], [(0, 'skipped'), (1, 'delivered')])
        self.assertEqual(calls, ['0', '1'])  # no retry after budget exhausted

    async def test_budget_is_shared_by_mt_and_tts_and_preserves_text(self):
        results = []
        async def translate(text):
            await asyncio.sleep(.02)
            return 'translated'
        async def tts(text): await asyncio.Event().wait()
        q = TranslationQueue(Config(translation_num_workers=1, translation_segment_timeout_s=.04),
                             'test', results.append, translate, tts)
        q.start()
        await q.enqueue(segment(0))
        await asyncio.wait_for(q.stop(), 1)
        self.assertEqual(results[0].state, 'skipped')
        self.assertEqual(results[0].translated_text, 'translated')
        self.assertIn('TTS', results[0].error)

    async def test_tts_total_deadline_even_with_continuous_messages(self):
        class Socket:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def send(self, data): pass
            async def recv(self):
                await asyncio.sleep(.001)
                return '{"audio": "YQ=="}'  # never isFinal; recv never stalls
        cfg = Config(elevenlabs_api_key='test', elevenlabs_voice_id='test', tts_total_timeout_s=.02)
        with patch('websockets.connect', return_value=Socket()):
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(synthesize_speech(cfg, 'hello'), .5)

    async def test_slow_failing_debug_does_not_block_event_loop(self):
        c = SessionCoordinator(Config(debug_audio_enabled=True))
        entered, release = threading.Event(), threading.Event()
        def writer(*args):
            entered.set()
            release.wait(1)
            raise OSError('disk full')
        with patch('app.session.write_debug_output', writer):
            c._schedule_debug(segment(0))
            for _ in range(100):
                if entered.is_set(): break
                await asyncio.sleep(.001)
            self.assertTrue(entered.is_set())
            self.assertEqual(len(c._debug_tasks), 1)
            # The thread is still blocked but this event-loop coroutine runs.
            release.set()
            await asyncio.gather(*list(c._debug_tasks))  # exception isolated

    async def test_debug_disabled_by_default(self):
        c = SessionCoordinator(Config())
        with patch('app.session.write_debug_output') as writer:
            c._schedule_debug(segment(0))
            await asyncio.sleep(0)
            writer.assert_not_called()

    async def test_detach_cancels_old_question_before_closing_pool(self):
        c = SessionCoordinator(Config())
        cancelled = asyncio.Event()
        async def question():
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        class Queue:
            async def stop(self):
                if not cancelled.is_set(): raise AssertionError('pool closed before question stopped')
        task = asyncio.create_task(question())
        c._question_tasks.add(task)
        c._translation_queue = Queue()
        await asyncio.sleep(0)
        await c._detach_current()
        self.assertTrue(task.cancelled())


if __name__ == '__main__':
    unittest.main()
