"""Offline HTTP TTS regressions; no API credentials or network required."""
import asyncio
import unittest
from unittest.mock import patch
from app.config import Config
from app.translation_pipeline import TranslationHTTPClient


class Content:
    def __init__(self, stall=False): self.stall = stall
    async def iter_any(self):
        yield b'first'
        if self.stall:
            await asyncio.Event().wait()
        yield b'second'


class Response:
    def __init__(self, status=200, stall=False):
        self.status = status
        self.content = Content(stall)
        self.closed = False
    async def __aenter__(self): return self
    async def __aexit__(self, *args): self.closed = True


class Session:
    def __init__(self):
        self.calls = []
        self.responses = []
        self.closed = False
        self.status = 200
        self.stall = False
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = Response(self.status, self.stall)
        self.responses.append(response)
        return response
    async def close(self): self.closed = True


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_pool_and_selects_question_voice(self):
        session = Session()
        cfg = Config(elevenlabs_api_key='fake', elevenlabs_voice_id='lecture')
        with patch('app.translation_pipeline.aiohttp.ClientSession', return_value=session) as factory:
            client = TranslationHTTPClient(cfg)
            self.assertEqual(await client.synthesize('hello'), b'firstsecond')
            await client.synthesize('question', 'question-voice')
            self.assertEqual(factory.call_count, 1)
            self.assertIn('/question-voice/stream', session.calls[1][0])
            self.assertEqual(session.calls[0][1]['json']['model_id'], 'eleven_flash_v2_5')
            await client.close()
            self.assertTrue(session.closed)
            with self.assertRaises(RuntimeError): await client.synthesize('late')

    async def test_http_failure_excludes_provider_body_and_key(self):
        session = Session()
        session.status = 401
        with patch('app.translation_pipeline.aiohttp.ClientSession', return_value=session):
            client = TranslationHTTPClient(Config(elevenlabs_api_key='secret', elevenlabs_voice_id='voice'))
            try:
                with self.assertRaisesRegex(RuntimeError, '^ElevenLabs HTTP 401$'):
                    await client.synthesize('hello')
                self.assertTrue(session.responses[0].closed)
            finally: await client.close()

    async def test_timeout_discards_partial_audio_and_closes_response(self):
        session = Session()
        session.stall = True
        with patch('app.translation_pipeline.aiohttp.ClientSession', return_value=session):
            client = TranslationHTTPClient(Config(elevenlabs_api_key='fake', elevenlabs_voice_id='voice', tts_total_timeout_s=.02))
            try:
                with self.assertRaises(asyncio.TimeoutError): await client.synthesize('hello')
                self.assertTrue(session.responses[0].closed)
            finally: await client.close()

    async def test_empty_response_is_failure(self):
        class EmptyContent:
            async def iter_any(self):
                yield b''
        session = Session()
        original = session.post
        def post(*args, **kwargs):
            response = original(*args, **kwargs)
            response.content = EmptyContent()
            return response
        session.post = post
        with patch('app.translation_pipeline.aiohttp.ClientSession', return_value=session):
            client = TranslationHTTPClient(Config(elevenlabs_api_key='fake', elevenlabs_voice_id='voice'))
            try:
                with self.assertRaisesRegex(RuntimeError, 'No audio chunks'):
                    await client.synthesize('hello')
            finally: await client.close()


if __name__ == '__main__': unittest.main()
