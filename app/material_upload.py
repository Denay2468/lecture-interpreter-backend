"""Authenticated, transient PDF processing with upload and execution limits."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from fastapi import HTTPException
from starlette.datastructures import UploadFile
from .materials import MAX_BYTES, MAX_FILES, automatic_terms


def extract_worker(data):
    try:
        result = subprocess.run([sys.executable, '-m', 'app.pdf_extract'], input=data,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                cwd=Path(__file__).resolve().parent.parent, timeout=20)
        if result.returncode:
            raise ValueError('PDF extraction failed or exceeded resource limits.')
        parsed = json.loads(result.stdout)
        if 'error' in parsed:
            raise ValueError(parsed['error'])
        return parsed
    except subprocess.TimeoutExpired:
        raise ValueError('PDF processing exceeded 20 seconds. Split the module or simplify the PDF.') from None


async def analyze_upload(request, language):
    if language not in {'id', 'en', 'zh', 'ja', 'es'}:
        raise HTTPException(400, 'Unsupported lecture language.')
    # Bound the request before multipart parsing, including chunked requests.
    # A spooled temporary stream avoids holding 100 MB in server RAM.
    limit = MAX_FILES * MAX_BYTES + 1024 * 1024
    size = 0
    with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as spool:
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, 'Upload exceeds the 100 MB batch limit.')
            spool.write(chunk)
        spool.seek(0)
        async def receive():
            data = spool.read(65536)
            return {'type': 'http.request', 'body': data, 'more_body': bool(data)}
        from starlette.requests import Request
        replay = Request(request.scope, receive)
        async with replay.form(max_files=MAX_FILES, max_fields=0, max_part_size=MAX_BYTES) as form:
            files = form.getlist('files')
            if not files or len(files) > MAX_FILES or len(form.multi_items()) != len(files):
                raise HTTPException(400, 'Upload 1–10 PDFs using the files field.')
            for file in files:
                if not isinstance(file, UploadFile) or not (file.filename or '').lower().endswith('.pdf'):
                    raise HTTPException(400, 'Only PDF files are accepted.')
                if file.size is not None and file.size > MAX_BYTES:
                    raise HTTPException(413, 'Each PDF must be at most 10 MB.')
            documents = []
            for file in files:
                data = await file.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise HTTPException(413, 'Each PDF must be at most 10 MB.')
                try:
                    result = await asyncio.to_thread(extract_worker, data)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from None
                name = (file.filename or 'module.pdf').replace('\\', '/').split('/')[-1][:160]
                documents.append({'name': name, **result})
    return {'documents': documents, 'automatic_term_count': len(automatic_terms(documents, language))}
