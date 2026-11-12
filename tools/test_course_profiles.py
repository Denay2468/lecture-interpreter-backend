"""Offline course selection, overrides, isolation and provider payload tests."""
import json
import unittest
from unittest.mock import patch
from app.config import Config
from app.course_profiles import CATALOG, resolve_course_glossary
from app.glossary import directional_pairs, protect, unprotect
from app.translation_pipeline import TranslationHTTPClient


class Profiles(unittest.TestCase):
    def test_catalog_all_courses_valid_and_distinct(self):
        records = json.loads(CATALOG.read_text())['courses']
        self.assertEqual(len(records), 6)
        for record in records:
            pairs, context = resolve_course_glossary(record['id'], '', 'id')
            self.assertEqual(len(pairs), 24)
            self.assertTrue(context)
        network, _ = resolve_course_glossary('networks', '', 'id')
        ai, _ = resolve_course_glossary('ai-data', '', 'id')
        self.assertEqual(unprotect(protect('jaringan', network)[0]), 'network')
        self.assertEqual(protect('jaringan', ai), ('jaringan', False))
        self.assertEqual(unprotect(protect('jaringan saraf', ai)[0]), 'neural network')

    def test_override_is_lecture_scoped(self):
        pairs, _ = resolve_course_glossary('networks', 'JARINGAN = computer network\nTCP = TCP', 'id')
        self.assertEqual(dict((s.casefold(), t) for s,t in pairs)['jaringan'], 'computer network')
        original, _ = resolve_course_glossary('networks', '', 'id')
        self.assertEqual(dict(original)['jaringan'], 'network')
        self.assertEqual(resolve_course_glossary('', '', 'id'), ((), ''))

    def test_invalid_requests_and_combined_limit(self):
        for profile, text, lang in [('bad', '', 'id'), (None, '', 'id'), ('networks', '', 'ja'),
                                    ('networks', 'x=y\nX=z', 'id'), ('', 'x'*8001, 'id'),
                                    ('networks', '\n'.join(f'term{i}=value{i}' for i in range(17)), 'id')]:
            with self.assertRaises(ValueError): resolve_course_glossary(profile, text, lang)

    def test_reverse_omits_ambiguous_entries_and_wrong_languages(self):
        pairs, _ = resolve_course_glossary('databases', '', 'id')
        reverse = directional_pairs(pairs, 'EN-US', 'ID', 'id')
        self.assertNotIn('database', dict(reverse)) # basis data / pangkalan data
        self.assertEqual(dict(reverse)['primary key'], 'kunci primer')
        self.assertEqual(directional_pairs(pairs, 'JA', 'EN', 'id'), ())
        self.assertEqual(directional_pairs(pairs, 'EN', 'ES', 'id'), ())
        self.assertEqual(directional_pairs(pairs, 'ID', 'EN-US', 'id'), pairs)


class Response:
    status = 200
    def __init__(self, payload): self.payload = payload
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def json(self): return {'translations': [{'text': self.payload['text']}]}

class Session:
    def __init__(self): self.calls = []
    def post(self, url, **kwargs):
        self.calls.append(kwargs['data'])
        return Response(kwargs['data'])
    async def close(self): pass


class HTTPGlossary(unittest.IsolatedAsyncioTestCase):
    async def test_live_provider_payload_both_directions_preserves_terms(self):
        pairs, context = resolve_course_glossary('networks', '', 'id')
        session = Session()
        with patch('app.translation_pipeline.aiohttp.ClientSession', return_value=session):
            client = TranslationHTTPClient(Config(course_glossary=pairs, course_context=context, deepl_api_key='fake'))
            try:
                result = await client.translate('Gua nggak ngerti kehilangan paket & latensi.', 'ID', 'EN-US')
                self.assertIn('packet loss & latency', result)
                self.assertIn('Saya tidak mengerti', result)
                self.assertEqual(session.calls[0]['ignore_tags'], 'blbterm')
                self.assertEqual(session.calls[0]['context'], context)
                result = await client.translate('Why does packet loss increase?', 'EN', 'ID')
                self.assertEqual(result, 'Why does kehilangan paket increase?')
                self.assertEqual(session.calls[1]['target_lang'], 'ID')
                # Other sessions retain the existing no-profile behaviour.
                plain = TranslationHTTPClient(Config(deepl_api_key='fake'))
                await plain.translate('halo')
                self.assertNotIn('context', session.calls[-1])
                await plain.close()
            finally: await client.close()

    async def test_bad_profile_rejected_before_teacher_attach(self):
        from app.server import Server
        class Socket:
            closed = None
            async def recv(self): return json.dumps({'type': 'configure_lecture', 'course_profile':'unknown'})
            async def close(self, **kwargs): self.closed = kwargs
        server = Server(Config())
        sock = Socket()
        await server._handle_teacher(sock, {'configure':['1'], 'source':['id']})
        self.assertEqual(sock.closed['code'], 4400)
        self.assertFalse(server._active['legacy'].coordinator.teacher_connected)
