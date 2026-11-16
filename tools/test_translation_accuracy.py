"""Regression tests for context/phrase handling, not a provider accuracy score."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from app.config import Config
from app.segmentation import Segment, Segmenter
from app.slang import normalize_indonesian
from app.translation_pipeline import TranslationHTTPClient, TranslationQueue
from tools.test_course_profiles import Session


class Boundaries(unittest.IsolatedAsyncioTestCase):
    async def test_early_cut_keeps_negation_and_technical_term_together(self):
        output = []
        cfg = Config(course_glossary=(('jaringan saraf', 'neural network'),))
        seg = Segmenter(cfg, 's', output.append)
        seg._dg_connection = type('Socket', (), {'send_finalize': AsyncMock()})()
        for text in ['hari ini kita memakai jaringan', 'komputer ini ternyata nggak']:
            await seg._maybe_finalize_phrase(text)
            await seg._maybe_finalize_phrase(text)
        seg._dg_connection.send_finalize.assert_not_awaited()
        await seg._on_stt_event('results', 'hari ini kita memakai jaringan', True)
        self.assertEqual(output, [])
        await seg._on_stt_event('results', 'saraf', True)
        self.assertEqual(output[0].transcript, 'hari ini kita memakai jaringan saraf')
        await seg._on_stt_event('results', 'komputer ini ternyata nggak', True)
        self.assertEqual(len(output), 1)
        await seg._on_stt_event('results', 'mahal', True)
        self.assertEqual(output[1].transcript, 'komputer ini ternyata nggak mahal')

    async def test_real_pause_still_flushes_incomplete_phrase(self):
        output = []
        seg = Segmenter(Config(), 's', output.append)
        await seg._on_stt_event('results', 'komputer ini ternyata tidak', True, True)
        self.assertEqual(output[0].transcript, 'komputer ini ternyata tidak')

    def test_slang_preserves_code_and_acronyms(self):
        self.assertEqual(normalize_indonesian('Gua ngecek `pake` dan GA, bukan ngapus.'),
                         'Saya memeriksa `pake` dan GA, bukan menghapus.')
        self.assertEqual(normalize_indonesian('cek `gua belum selesai'), 'cek `gua belum selesai')


class Context(unittest.IsolatedAsyncioTestCase):
    async def test_payload_does_not_translate_context_or_override_terms(self):
        session = Session()
        with patch('app.translation_pipeline.aiohttp.ClientSession', return_value=session):
            client = TranslationHTTPClient(Config(deepl_api_key='fake', course_context='Kuliah IT.',
                                                  course_glossary=(('ngecek', 'validate'),)))
            output = await client.translate('Gua ngecek paket.', 'id', 'en-us', context='Kita membahas jaringan.')
            self.assertEqual(output, 'Saya validate paket.')
            self.assertEqual(session.calls[0]['context'], 'Kuliah IT.\nKita membahas jaringan.')
            self.assertNotIn('membahas', session.calls[0]['text'])
            self.assertEqual(client.mt_characters, len('Gua ngecek paket.'))
            await client.close()

    async def test_parallel_workers_retry_same_prior_context_and_isolate_questions(self):
        calls = []
        first_started, release_first = asyncio.Event(), asyncio.Event()
        async def translate(text, **kwargs):
            calls.append((text, kwargs.get('context', '')))
            if text == 'pertama' and calls.count(('pertama', '')) == 1:
                first_started.set()
                await release_first.wait()
                raise TimeoutError()
            if text == 'kedua':
                release_first.set()
            return text
        cfg = Config(translation_num_workers=2)
        q = TranslationQueue(cfg, 'a', on_translated=lambda _: None,
                             synthesize_fn=AsyncMock(return_value=b'audio'))
        q._http.translate = translate
        other = TranslationQueue(cfg, 'b', on_translated=lambda _: None)
        q.start()
        try:
            await q.enqueue(Segment('a', 0, b'', 'pertama', 'speech_final', 1))
            await asyncio.wait_for(first_started.wait(), 1)
            await q.enqueue(Segment('a', 1, b'', 'kedua', 'speech_final', 1))
            await asyncio.wait_for(q._queue.join(), 2)
            await q.translate_question('Why?')
            self.assertEqual(calls, [('pertama', ''), ('kedua', 'pertama'), ('pertama', ''), ('Why?', '')])
            self.assertEqual(other._source_history, '')
            self.assertEqual(q.segments_delivered, 2)
        finally:
            release_first.set()
            await q.stop()
            await other.stop()
        self.assertEqual(q._source_history, '')

    async def test_context_bounded_and_silence_does_not_erase_history(self):
        q = TranslationQueue(Config(), 's')
        for seq, text in enumerate(['x' * 2000, '', 'terakhir']):
            await q.enqueue(Segment('s', seq, b'', text, 'speech_final', 1))
        items = [q._queue.get_nowait() for _ in range(3)]
        self.assertEqual(items[0][2], '')
        self.assertEqual(items[1][2], 'x' * 1500)
        self.assertEqual(items[2][2], 'x' * 1500)
        self.assertLessEqual(len(q._source_history), 1500)
        for _ in items: q._queue.task_done()
        await q.stop()
