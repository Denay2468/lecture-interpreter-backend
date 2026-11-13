"""Offline classroom, phrase and streaming regressions; no API keys required."""
import asyncio
import base64
import json
import time
import unittest
from unittest.mock import patch
from app.classroom import Classroom
from app.config import Config
from app.glossary import parse_glossary, protect, unprotect
from app.slang import normalize_indonesian
from app.playback import StudentPlaybackController, StreamingSegment
from app.segmentation import Segment, Segmenter
from app.session import SessionCoordinator
from app.translation_pipeline import TranslationHTTPClient, TranslationQueue, TranslatedSegment


class Socket:
    def __init__(self): self.messages = []; self.closed = None
    async def send(self, message): self.messages.append(message)
    async def close(self, **kwargs): self.closed = kwargs


class Coord(SessionCoordinator):
    def __init__(self):
        super().__init__(Config())
        self._playback = StudentPlaybackController('s', include_captions=True)
        self.presses = self.releases = self.frames = 0
    async def handle_ask_press(self): self.presses += 1
    async def handle_ask_release(self): self.releases += 1
    def handle_question_audio_frame(self, data): self.frames += 1
    async def handle_student_disconnect(self): pass


class ClassroomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.coord = Coord(); self.hub = Classroom(self.coord)
    async def asyncTearDown(self): await self.hub.close()
    async def test_broadcast_same_segment_to_two_students(self):
        ids = [await self.hub.join(Socket()) for _ in range(2)]
        self.coord.current_playback.submit(TranslatedSegment('s', 0, 'halo', 'hello', b'audio', 'delivered'))
        await asyncio.sleep(.02)
        for student in ids:
            events = []
            q = self.hub.students[student]['queue']
            while not q.empty(): events.append(q.get_nowait())
            pair = next(e for e in events if isinstance(e, tuple))
            self.assertEqual(pair[0]['translated_text'], 'hello')
            self.assertTrue(pair[1].endswith(b'audio'))
    async def test_permission_owner_and_disconnect_isolation(self):
        a, b = [await self.hub.join(Socket()) for _ in range(2)]
        await self.hub.request(a); await self.hub.request(a); await self.hub.request(b)
        self.assertEqual(self.hub.waiting, [a,b])
        await self.hub.grant(a)
        await self.hub.receive(b, b'bad')
        await self.hub.receive(b, '{"type":"ask_release"}')
        await self.hub.receive(a, '{"type":"ask_press"}')
        await self.hub.receive(a, b'ok')
        await self.hub.leave(b)
        self.assertEqual((self.coord.frames, self.coord.presses, self.coord.releases), (1,1,0))
        self.assertEqual(self.hub.owner, a)
    async def test_slow_student_cannot_block_room(self):
        a,b = [await self.hub.join(Socket()) for _ in range(2)]
        for n in range(40): self.hub.put(a, {'n':n})
        self.assertTrue(self.hub.students[a]['overflow'].is_set())
        self.assertFalse(self.hub.students[b]['overflow'].is_set())
    async def test_question_result_only_reaches_owner(self):
        a,b = [await self.hub.join(Socket()) for _ in range(2)]
        self.hub.owner = a
        for student in (a,b):
            q=self.hub.students[student]['queue']
            while not q.empty(): q.get_nowait()
        await self.hub.control({'type':'question_delivered'})
        self.assertEqual(self.hub.students[a]['queue'].qsize(), 1)
        self.assertTrue(self.hub.students[b]['queue'].empty())
    async def test_stream_order_and_caption_survives_error(self):
        student = await self.hub.join(Socket())
        second = StreamingSegment(1, asyncio.Queue(), time.monotonic())
        first = StreamingSegment(0, asyncio.Queue(), time.monotonic())
        self.coord.current_playback.submit(second)
        self.coord.current_playback.submit(first)
        for stream in (first, second):
            await stream.events.put({'type':'segment','sequence':stream.sequence,'translated_text':'text'})
            await stream.events.put({'type':'segment_end','sequence':stream.sequence,'audio_failed':True})
        await asyncio.sleep(.02)
        q=self.hub.students[student]['queue']; events=[]
        while not q.empty(): events.append(q.get_nowait())
        self.assertEqual([e['sequence'] for e in events if e['type']=='segment'],[0,1])
    async def test_duplicate_playback_reports_not_counted(self):
        student = await self.hub.join(Socket())
        self.hub.students[student]['sent'][2] = time.monotonic()-.1
        for _ in range(2): await self.hub.receive(student, '{"type":"playback_started","sequence":2}')
        self.assertEqual(len(self.hub.played_ms),1)


class LanguageTests(unittest.TestCase):
    def test_slang_boundaries_and_original_acronyms(self):
        self.assertEqual(normalize_indonesian('Gua nggak ngerti gimana jaringan GA bekerja.'),
                         'Saya tidak mengerti bagaimana jaringan GA bekerja.')
        self.assertEqual(normalize_indonesian('lagak JavaScript anjir'), 'lagak JavaScript anjir')
    def test_glossary_escaping_longest_and_boundaries(self):
        pairs=parse_glossary('jaringan = network\njaringan komputer = computer network\ntektokan = back-and-forth conversation')
        xml, tagged=protect('jaringan komputer & tektokan <test>', pairs)
        self.assertTrue(tagged)
        self.assertEqual(unprotect(xml),'computer network & back-and-forth conversation <test>')
        self.assertEqual(protect('penjaringan',pairs),('penjaringan',False))
    def test_glossary_rejects_duplicates_and_invalid(self):
        for text in ('halo=hello\nHALO=hi','no equals', 'x=' ):
            with self.assertRaises(ValueError): parse_glossary(text)


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_chunk_arrives_before_provider_finishes(self):
        events=[]; gate=asyncio.Event(); first=asyncio.Event()
        async def callback(seq,event):
            events.append(event)
            if event['type']=='audio_chunk': first.set()
        queue=TranslationQueue(Config(), 's', on_translated=lambda r:None,
                               translate_fn=lambda t: asyncio.sleep(0,result='hello'), on_stream=callback)
        async def stream(text, on_chunk):
            await on_chunk(b'\x00\x00'); await gate.wait(); await on_chunk(b'\x01\x00')
        queue._http.stream_pcm=stream
        queue.start()
        await queue.enqueue(Segment('s',0,b'00','halo','speech_final',20))
        await asyncio.wait_for(first.wait(),1)
        self.assertFalse(any(e['type']=='segment_end' for e in events))
        gate.set(); await queue.stop()
        self.assertEqual(events[-1]['state'],'delivered')
    async def test_partial_failure_not_retried_and_text_retained(self):
        events=[]; results=[]; calls=[]
        async def event(seq,data): events.append(data)
        queue=TranslationQueue(Config(), 's', on_translated=results.append,
                               translate_fn=lambda t:asyncio.sleep(0,result='hello'), on_stream=event)
        async def stream(text,on_chunk):
            calls.append(text); await on_chunk(b'00'); raise RuntimeError('stream stopped')
        queue._http.stream_pcm=stream
        queue.start(); await queue.enqueue(Segment('s',0,b'00','halo','speech_final',20)); await queue.stop()
        self.assertEqual(len(calls),1)
        self.assertEqual(results[0].translated_text,'hello')
        self.assertTrue(events[-1]['audio_failed'])
    async def test_stable_final_phrase_commits_without_speech_final(self):
        out=[]; segmenter=Segmenter(Config(), 's', on_segment=out.append)
        segmenter._current_audio.extend(b'00')
        await segmenter._on_stt_event('results','hari ini kita belajar tentang jaringan komputer',True,False)
        self.assertEqual(out[0].trigger,'stable_phrase')
        await segmenter._on_stt_event('results','yang menghubungkan perangkat',True,True)
        self.assertEqual(out[1].transcript,'yang menghubungkan perangkat')
        self.assertNotIn('hari ini',out[1].transcript)
    async def test_interim_is_never_spoken(self):
        out=[]; segmenter=Segmenter(Config(), 's', on_segment=out.append)
        segmenter._current_audio.extend(b'00')
        await segmenter._on_stt_event('results','hari ini kita belajar tentang jaringan komputer',False,False)
        self.assertEqual(out,[])
    async def test_same_language_bypasses_mt(self):
        client=TranslationHTTPClient(Config())
        self.assertEqual(await client.translate('hello','EN','EN-US'),'hello')
        self.assertIsNone(client._session)
    async def test_language_routing_both_directions(self):
        for lang,target in [('id','ID'),('zh','ZH-HANS'),('ja','JA'),('es','ES')]:
            async def synthesize(text): return b'audio'
            q=TranslationQueue(Config(segmentation_language=lang),'s',
                               on_translated=lambda _: None, synthesize_fn=synthesize)
            calls=[]
            async def translate(text,source_lang='ID',target_lang='EN-US', **kwargs):
                calls.append((source_lang,target_lang)); return text
            q._http.translate=translate
            await q._process_segment(Segment('s', 0, b'', 'test', 'speech_final', 1))
            await q.translate_question('question')
            self.assertEqual(calls,[(lang.upper(),'EN-US'),('EN',target)])

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_websockets_broadcast_stream_before_end(self):
        import websockets
        from app.server import Server, LEGACY_KEY
        server=Server(Config(),coordinator_factory=lambda cfg:Coord())
        async with websockets.serve(server._router,'127.0.0.1',0) as listener:
            port=listener.sockets[0].getsockname()[1]
            url=f'ws://127.0.0.1:{port}/ws/student'
            async with websockets.connect(url) as a, websockets.connect(url) as b:
                async def receive_type(ws,kind):
                    while True:
                        value=json.loads(await asyncio.wait_for(ws.recv(),1))
                        if value['type']==kind:return value
                await receive_type(a,'mode_changed');await receive_type(b,'mode_changed')
                stream=StreamingSegment(0,asyncio.Queue(),time.monotonic())
                server._active[LEGACY_KEY].coordinator.current_playback.submit(stream)
                await stream.events.put({'type':'segment','sequence':0,'translated_text':'hello'})
                await stream.events.put({'type':'audio_chunk','sequence':0,'pcm':'AAA='})
                self.assertEqual(await receive_type(a,'audio_chunk'),await receive_type(b,'audio_chunk'))
                await stream.events.put({'type':'segment_end','sequence':0})
                await receive_type(a,'segment_end');await receive_type(b,'segment_end')
            await server._active[LEGACY_KEY].classroom.close()

    async def test_pcm_provider_forwards_even_samples_and_hides_error_body(self):
        chunks=[]
        class Response:
            status=200
            async def __aenter__(self):return self
            async def __aexit__(self,*args):self.closed=True
            @property
            def content(self):return self
            async def iter_chunked(self,size):
                yield b'\x00'
                yield b'\x01\x02\x03'
        class Pool:
            def post(self,*args,**kwargs):self.options=kwargs;return response
        response=Response();pool=Pool()
        client=TranslationHTTPClient(Config(elevenlabs_api_key='test',elevenlabs_voice_id='voice'))
        client._session=pool
        async def chunk(data):chunks.append(data)
        await client.stream_pcm('hello',chunk)
        self.assertEqual(b''.join(chunks),b'\x00\x01\x02\x03')
        self.assertEqual(pool.options['params']['output_format'],'pcm_24000')
        self.assertTrue(response.closed)
        response.status=401
        with self.assertRaisesRegex(RuntimeError,'ElevenLabs HTTP 401'):
            await client.stream_pcm('hello',chunk)


class MetricTests(unittest.TestCase):
    def test_percentiles_and_unknown_cost(self):
        from app.telemetry import percentile, estimate_cost
        self.assertIsNone(percentile([], 95))
        self.assertEqual(percentile(list(range(1,21)),95),19)
        self.assertIsNone(estimate_cost(60,100,100,(None,1,1)))
        self.assertAlmostEqual(estimate_cost(60,1_000_000,1_000_000,(1,2,3)),6)
