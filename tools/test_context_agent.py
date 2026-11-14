import asyncio
import json
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from app.config import Config
from app.context_agent import ContextAgent
from app.translation_pipeline import TranslationQueue
from app.segmentation import Segment


class BackgroundContext(unittest.IsolatedAsyncioTestCase):
    def cfg(self, **kw):
        return Config(context_agent_enabled=True, context_agent_interval_s=.02,
                      context_agent_timeout_s=.05, **kw)

    async def test_disabled_never_calls_provider(self):
        fn = AsyncMock()
        agent = ContextAgent(Config(), fn)
        agent.submit('jaringan komputer')
        await asyncio.sleep(0)
        fn.assert_not_called()
        self.assertIsNone(agent._task)
        await agent.close()

    async def test_latest_pending_only_and_shutdown(self):
        gate = asyncio.Event()
        calls = []
        async def analyze(text, refs):
            calls.append(text)
            await gate.wait()
            return {'topic':'Networks', 'summary':text, 'confidence':.9}
        agent = ContextAgent(self.cfg(), analyze)
        agent.submit('jaringan komputer pertama')
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        for i in range(50): agent.submit('jaringan komputer ' + str(i))
        self.assertEqual(agent._pending.qsize(), 1)
        gate.set()
        await asyncio.sleep(.04)
        self.assertEqual(calls, ['jaringan komputer pertama', 'jaringan komputer 49'])
        self.assertIn('49', agent.snapshot('jaringan komputer'))
        self.assertEqual(agent.snapshot('anatomi tulang manusia'), '')
        await agent.close()
        self.assertTrue(agent._task.done())
        self.assertEqual(agent.snapshot('jaringan komputer'), '')

    async def test_timeout_malformed_and_low_confidence_fail_open(self):
        async def slow(*args): await asyncio.sleep(1)
        for fn in [slow, AsyncMock(return_value={'topic':'x','summary':'bad','confidence':.2}),
                   AsyncMock(return_value=None), AsyncMock(return_value={'topic':'x','summary':'bad','confidence':float('nan')})]:
            agent=ContextAgent(self.cfg(),fn)
            agent.submit('jaringan komputer')
            await asyncio.sleep(.07)
            self.assertEqual(agent.snapshot('jaringan komputer'), '')
            await agent.close()

    async def test_expiration_and_classroom_isolation(self):
        result={'topic':'Networks','summary':'Network topology','confidence':.9}
        a=ContextAgent(self.cfg(context_agent_max_age_s=.03),AsyncMock(return_value=result))
        b=ContextAgent(self.cfg(),AsyncMock(return_value=result))
        a.submit('jaringan komputer')
        await asyncio.sleep(.01)
        self.assertTrue(a.snapshot('jaringan'))
        self.assertEqual(b.snapshot('jaringan'), '')
        await asyncio.sleep(.04)
        self.assertEqual(a.snapshot('jaringan'), '')
        await a.close(); await b.close()

    async def test_enqueue_does_not_wait_and_freezes_context(self):
        q=TranslationQueue(self.cfg(), 's')
        async def slow(*args): await asyncio.sleep(1)
        q._context_agent._analyzer=slow
        q._context_agent.enabled=True
        q._context_agent._result=('Networks: packet routing', frozenset({'jaringan'}),time.monotonic())
        # Build using the real Segment fields.
        segment=Segment(session_id='s',sequence=0,transcript='jaringan komputer',audio=b'',duration_ms=1000,trigger='test')
        await asyncio.wait_for(q.enqueue(segment), .05)
        item=q._queue.get_nowait();q._queue.task_done()
        self.assertIn('packet routing', item[2])
        q._context_agent._result=('Medicine: tissue',frozenset({'jaringan'}),time.monotonic())
        self.assertNotIn('tissue',item[2])
        await q.stop()

    async def test_empty_segment_does_not_trigger_analysis(self):
        q=TranslationQueue(Config(), 's')
        q._source_history='previous speech'
        from unittest.mock import Mock
        q._context_agent.submit=Mock()
        segment=Segment(session_id='s',sequence=0,audio=b'',transcript='',duration_ms=1000,trigger='test')
        await q.enqueue(segment)
        q._context_agent.submit.assert_not_called()
        q._queue.get_nowait(); q._queue.task_done()
        await q.stop()

    async def test_provider_contract_and_response_limit(self):
        output = {'topic': 'Networks', 'summary': 'Packet routing', 'confidence': .9}
        raw = json.dumps({'choices':[{'message': {'content': json.dumps(output)}}]}).encode()
        class Response:
            status = 200
            content = None
            def __init__(self): self.content = self
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def iter_chunked(self, size):
                yield raw
        class Session:
            def __init__(self): self.requests = []; self.closed = False
            def post(self, url, **kwargs):
                self.requests.append((url, kwargs))
                return Response()
            async def close(self): self.closed = True
        session = Session()
        cfg = self.cfg(context_agent_url='https://provider.example/chat/completions', context_agent_model='test-model', context_agent_api_key='secret')
        with patch('app.context_agent.aiohttp.ClientSession', return_value=session):
            agent=ContextAgent(cfg)
            self.assertEqual(await agent._request('jaringan komputer', 'reference'), output)
            request = session.requests[0][1]
            self.assertEqual(request['headers']['Authorization'], 'Bearer secret')
            content = json.loads(request['json']['messages'][1]['content'])
            self.assertEqual(content['recent_speech'], 'jaringan komputer')
            raw = b'x' * 16385
            with self.assertRaises(ValueError): await agent._request('x', '')
            await agent.close()
            self.assertTrue(session.closed)

    def test_lecturer_ui_has_only_materials_not_manual_terms(self):
        root=Path(__file__).resolve().parents[1]
        html=(root/'frontend/teacher.html').read_text()
        js=(root/'frontend/js/teacher.js').read_text()
        self.assertIn('id="course-pdfs"',html)
        for id in ['course-profile','course-department','course-preview','course-glossary']:
            self.assertNotIn('id="'+id+'"',html)
        self.assertNotIn('BLB.courseProfiles',js)
