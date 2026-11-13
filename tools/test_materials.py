"""Offline PDF upload, authorization, retrieval and lecture configuration tests."""
import asyncio
import io
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
from pypdf import PdfWriter
from app import auth
from app.api import create_app
from app.config import Config
from app.db import AuthUser
from app.materials import validate_materials, index_materials, relevant_context, automatic_terms
from app.pdf_extract import extract
from app.translation_pipeline import TranslationHTTPClient
from tools.test_course_profiles import Session


def text_pdf(text='Kehilangan paket terjadi pada jaringan komputer.'):
    # Minimal uncompressed PDF fixture: no renderer or provider needed.
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    content = DecodedStreamObject()
    content.set_data(('BT /F1 12 Tf 40 700 Td (' + text + ') Tj ET').encode('ascii'))
    page[NameObject('/Contents')] = writer._add_object(content)
    output = io.BytesIO(); writer.write(output); return output.getvalue()


class Extraction(unittest.TestCase):
    def test_native_text_and_worker(self):
        from app.material_upload import extract_worker
        result = extract_worker(text_pdf())
        self.assertIn('Kehilangan paket', result['text'])
        self.assertEqual(result['pages'], 1)

    def test_invalid_locked_scanned_and_page_limit(self):
        with self.assertRaises(ValueError): extract(b'not pdf')
        for count, password in [(1, None), (101, None), (1, 'secret')]:
            w = PdfWriter()
            for _ in range(count): w.add_blank_page(width=100, height=100)
            if password: w.encrypt(password)
            out = io.BytesIO(); w.write(out)
            with self.assertRaises(ValueError): extract(out.getvalue())

    def test_material_limits_and_domain_retrieval(self):
        with self.assertRaises(ValueError): validate_materials([{'name':'a','text':'b'}] * 11)
        with self.assertRaises(ValueError): validate_materials([{'name':'a','text':'x'*100001}])
        with self.assertRaises(ValueError): validate_materials('not a list')
        docs = validate_materials([{'name':'a.pdf','text':'Kehilangan paket terjadi pada jaringan komputer. ' * 10},
                                   {'name':'b.pdf','text':'Insulin regulates glucose in the bloodstream.'}])
        pairs = automatic_terms(docs, 'id')
        self.assertIn(('kehilangan paket','packet loss'), pairs)
        self.assertEqual(automatic_terms(docs, 'ja'), ())
        passages = index_materials(docs)
        self.assertIn('Kehilangan', relevant_context(passages, 'Why packet loss?', pairs=pairs))
        self.assertIn('Insulin', relevant_context(passages, 'glucose'))
        self.assertEqual(relevant_context(passages, 'completely unrelated'), '')
        self.assertEqual(relevant_context((), 'glucose'), '')
        # Equations and variable assignments must never become forced translations.
        self.assertEqual(automatic_terms([{'name':'a','text':'force = mass\nfoo = bar'}], 'id'), ())


class Upload(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.uid, self.iid = uuid.uuid4(), uuid.uuid4()
        self.user = AuthUser(self.uid, self.iid, 'lecturer@test.example', 'Lecturer', 'LECTURER')
        self.cfg = Config(jwt_secret='a-test-only-secret-long-enough-to-sign')
        self.db = SimpleNamespace(get_active_user=AsyncMock(return_value=self.user))
        self.app = create_app(self.cfg, self.db, None)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test')
        self.headers = {'Authorization': 'Bearer ' + auth.issue_access_token(self.uid, 'LECTURER', self.iid, self.cfg.jwt_secret)}
    async def asyncTearDown(self): await self.client.aclose()

    async def test_upload_and_auth(self):
        url = '/materials/analyze?language=id'
        self.assertEqual((await self.client.post(url)).status_code, 401)
        self.db.get_active_user.return_value = AuthUser(self.uid, self.iid, 's@test.example','Student','STUDENT')
        token = auth.issue_access_token(self.uid,'STUDENT',self.iid,self.cfg.jwt_secret)
        self.assertEqual((await self.client.post(url, headers={'Authorization':'Bearer '+token})).status_code,403)
        self.db.get_active_user.return_value = self.user
        response = await self.client.post(url, headers=self.headers,files=[('files',('lecture.pdf',text_pdf(),'application/pdf'))])
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('Kehilangan',response.json()['documents'][0]['text'])
        self.assertGreater(response.json()['automatic_term_count'],0)

    async def test_file_count_size_and_fake_pdf_rejected(self):
        for files, expected in [([('files',('x.pdf',text_pdf(),'application/pdf'))]*11,400),
                                ([('files',('x.pdf',b'x'*(10*1024*1024+1),'application/pdf'))],413),
                                ([('files',('x.pdf',b'not a pdf','application/pdf'))],422),
                                ([('files',('x.txt',text_pdf(),'application/pdf'))],400)]:
            result=await self.client.post('/materials/analyze',headers=self.headers,files=files)
            self.assertEqual(result.status_code,expected,result.text)

    async def test_both_directions_use_passages_without_translating_whole_pdf(self):
        docs=[{'name':'module.pdf','text':'Kehilangan paket terjadi pada jaringan komputer.'}]
        cfg=Config(deepl_api_key='fake',material_passages=index_materials(docs),course_glossary=automatic_terms(docs,'id'))
        session=Session()
        with patch('app.translation_pipeline.aiohttp.ClientSession',return_value=session):
            client=TranslationHTTPClient(cfg)
            await client.translate('kehilangan paket','ID','EN')
            await client.translate('Why packet loss?', 'EN','ID')
            for call in session.calls:
                self.assertIn('jaringan komputer',call['context'])
                self.assertNotIn('jaringan komputer',call['text'])
            await client.close()

    async def test_invalid_module_configuration_rejected(self):
        import json
        from app.server import Server
        class Socket:
            async def recv(self): return json.dumps({'type':'configure_lecture','materials':[{'name':'x','text':'a'}]*11})
            async def close(self, **kwargs): self.closed=kwargs
        socket=Socket(); server=Server(Config())
        await server._handle_teacher(socket,{'configure':['1'],'source':['id']})
        self.assertEqual(socket.closed['code'],4400)
