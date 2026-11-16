"""Isolated PDF text worker. Never renders or executes embedded PDF content."""
import io
import json
import sys
from pypdf import PdfReader
from .materials import MAX_BYTES, MAX_PAGES, MAX_TEXT


def extract(data):
    if len(data) > MAX_BYTES:
        raise ValueError('PDF exceeds 10 MB.')
    if not data.startswith(b'%PDF-'):
        raise ValueError('This file is not a PDF.')
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise ValueError('Password-protected PDFs are not supported. Upload an unlocked copy.')
    if len(reader.pages) > MAX_PAGES:
        raise ValueError('PDF exceeds 100 pages. Split the module into smaller PDFs.')
    parts, total, empty = [], 0, 0
    for page in reader.pages:
        text = page.extract_text() or ''
        text = text.replace('\x00', '').strip()
        if len(text) < 10:
            empty += 1
        total += len(text) + 1
        if total > MAX_TEXT:
            raise ValueError('PDF exceeds 100,000 text characters. Split the module into smaller PDFs.')
        parts.append(text)
    text = '\n'.join(parts).strip()
    if len(text) < 10:
        raise ValueError('No readable text found. Scanned PDFs need OCR before upload.')
    return {'text': text, 'pages': len(reader.pages), 'pages_without_text': empty}


if __name__ == '__main__':
    # Cap worker address space on Unix. Windows still has the parent timeout
    # and input/page/text limits; use OS/container limits in production too.
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
    except ImportError:
        pass
    try:
        result = extract(sys.stdin.buffer.read(MAX_BYTES + 1))
    except ValueError as exc:
        result = {'error': str(exc)}
    except Exception:
        result = {'error': 'PDF could not be read. Upload a valid text PDF.'}
    print(json.dumps(result, ensure_ascii=True))
