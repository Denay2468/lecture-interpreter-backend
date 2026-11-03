"""Early finalization tests without provider calls or keys."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from app.config import Config
from app.segmentation import Segmenter


def result(text, final=False, speech=False):
    return NS(type='Results', is_final=final, speech_final=speech,
              channel=NS(alternatives=[NS(transcript=text)]))


class PhraseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.out = []
        self.seg = Segmenter(Config(), 's', self.out.append)
        self.seg._dg_connection = NS(send_finalize=AsyncMock())

    async def draft(self, text):
        await self.seg._on_dg_message(result(text))

    async def test_final_before_pause_and_no_duplicate_drafts(self):
        await self.draft('hari ini kita belajar jaringan')
        await self.draft('hari ini kita belajar jaringan komputer')
        self.seg._dg_connection.send_finalize.assert_awaited_once()
        self.assertEqual(self.out, [])
        await self.seg._on_dg_message(result('Hari ini kita belajar jaringan komputer', True))
        await self.seg._on_dg_message(result('dan internet', True, True))
        self.assertEqual([x.transcript for x in self.out],
                         ['Hari ini kita belajar jaringan komputer', 'dan internet'])
        self.assertEqual(self.out[0].trigger, 'stable_phrase')

    async def test_revised_prefix_does_not_trigger(self):
        await self.draft('harga komputer ini sangat murah')
        await self.draft('harga komputer itu sangat mahal')
        self.seg._dg_connection.send_finalize.assert_not_awaited()

    async def test_negation_waits_for_complement(self):
        await self.draft('komputer yang ini tidak')
        await self.draft('komputer yang ini tidak')
        self.seg._dg_connection.send_finalize.assert_not_awaited()
        await self.draft('komputer yang ini tidak mahal')
        self.seg._dg_connection.send_finalize.assert_awaited_once()

    async def test_pending_request_throttled_and_recovers_without_ack(self):
        with patch('app.segmentation._now_ms', return_value=1000):
            for _ in range(5): await self.draft('hari ini kita belajar jaringan')
        self.seg._dg_connection.send_finalize.assert_awaited_once()
        with patch('app.segmentation._now_ms', return_value=3100):
            await self.draft('hari ini kita belajar jaringan komputer')
        self.assertEqual(self.seg._dg_connection.send_finalize.await_count, 2)

    async def test_send_failure_keeps_normal_finalization(self):
        self.seg._dg_connection.send_finalize.side_effect = RuntimeError('secret')
        await self.draft('hari ini kita belajar jaringan')
        with self.assertLogs('segmentation', level='WARNING') as logs:
            await self.draft('hari ini kita belajar jaringan')
        self.assertNotIn('secret', str(logs.output))
        await self.seg._on_dg_message(result('hari ini kita belajar jaringan', True, True))
        self.assertEqual(len(self.out), 1)

    async def test_disabled_and_closed_do_not_request(self):
        self.seg._cfg.incremental_phrases_enabled = False
        for _ in range(2): await self.draft('hari ini kita belajar jaringan')
        self.seg._cfg.incremental_phrases_enabled = True
        self.seg.closed = True
        for _ in range(2): await self.draft('hari ini kita belajar jaringan')
        self.seg._dg_connection.send_finalize.assert_not_awaited()
