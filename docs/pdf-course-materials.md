# Automatic PDF course context

On teacher.html, section 3 accepts up to 10 PDF modules of 10 MiB each (UI: 10 MB).
Selecting files starts processing automatically; the lecturer does not need to
approve extracted terms. Start lecture is guarded until processing finishes.
Remove a module or clear all modules to change the next lecture's context.
Selecting a new batch replaces the previous selection. Changes during a live
lecture apply only after ending it and starting a new lecture.

## What it does

Authenticated lecturers/admins upload multipart PDFs to POST /materials/analyze.
Text extraction occurs locally on the backend in an isolated subprocess; no new
AI API key is needed. The originals and temporary upload files are discarded.
Extracted text stays in the current browser page, then is copied into the lecture
configuration. It is not written to database, localStorage or sessionStorage.
Reload/sign-out removes the page's module selection; there is no persistent
module library in this release.

During translation, lexical retrieval selects up to two relevant overlapping
passages (up to 1,600 characters) from those modules. DeepL receives these as
context, alongside course context and recent lecturer speech, rather than
translating the entire PDF for every spoken phrase. Both lecture and student
question translation use this mechanism. No extra DeepL request is made per
phrase. Extraction and passage indexing happen before the lecture.

For Indonesian IT material, existing attested IT catalog terms appearing in the
PDF can activate automatically, even without selecting an IT preset. An IT
subject anchor is required to avoid applying a network-computing sense to a
medical document merely mentioning jaringan. A selected course preset and manual
lecturer overrides take precedence. Conflicting catalog definitions are omitted.
Unknown terms and other majors use the retrieved PDF context; this release does
not generate a new bilingual dictionary with an LLM or guarantee every term is
recognized. Equations in a module are not treated as translation instructions.

## Limits and behavior

- 10 PDFs per batch and lecture, 10 MiB per PDF, 100 pages per PDF.
- Up to 100,000 extracted text characters per PDF; larger documents must be split.
- Password-protected, corrupt, non-PDF, and textless/scanned PDFs are rejected.
- Mixed text/image PDFs report pages with little/no readable text; images,
  diagrams and formulas are not interpreted. Use an OCR-processed PDF if needed.
- Each extraction subprocess has a 20-second timeout. Unix workers also have a
  768 MiB address-space cap. Windows production deployments should set OS/container
  memory limits; the Python worker cannot enforce that Unix cap on Windows.
- One upload batch at a time per server process; overlaps return 429. Each user
  can attempt 10 batches/hour. Rate/concurrency limits are per process.
- Uploaded PDFs are never made publicly downloadable. Relevant extracted text is
  sent to the configured translation provider during use; upload only material
  you are authorized to process there.
- No OCR service, STT model retraining, keyword boosting, PDF viewer or permanent
  module storage is included. Translation can still be wrong. A wrong source
  transcript, ambiguous vocabulary or missing material needs separate diagnosis.
- The WebSocket maximum message size is 8 MiB to accommodate bounded module text
  while preventing unlimited messages. PDF bytes never travel over that socket.

## Install and test

Stop the backend, install dependencies, then restart:

```powershell
python -m pip install -r requirements.txt
python -m unittest tools.test_materials tools.test_translation_accuracy tools.test_course_profiles tools.test_classroom_streaming -v
python run_server.py
```

Keep the frontend server running and hard-refresh teacher.html. Upload requires
normal lecturer/admin sign-in and the HTTP API; legacy single-session mode has
no upload API. Both .env and database credentials are unchanged.

Manual comparison: upload today's module, speak technical sentences inside and
outside that material, and capture expected/source/translated text. Repeat with
no module using the same audio. Record term accuracy and first-audio latency.
Offline tests validate boundaries, scope, upload/auth limits and API payloads;
they do not measure live linguistic accuracy or prove zero mistranslations.
