# IT course terminology

## Use in a lecture

1. Update the frontend and backend together, then restart `python run_server.py`.
2. Serve the frontend from the project root with `python -m http.server 8080 --directory frontend` in another terminal. Open `http://localhost:8080/teacher.html` (or `teacher.html?legacy=1` for a server without a database).
3. Choose **Indonesian** and a course in **IT course terminology**.
4. Expand **Review preset terminology**. These are editable starter translations, not an academically validated dictionary.
5. Add `source term = English term` lines in **Additional terms / overrides**. A matching source overrides its preset translation; new sources extend it. The combined limit remains 40 terms and 8,000 serialized characters. Selecting **Custom glossary only** removes the preset but keeps the lecturer's custom text.
6. Start the lecture. Settings are captured at Start and apply to that session. End and restart to change terminology.

Six seeds contain 24 entries each: Computer Networks, Programming, Databases, Software Engineering, Cybersecurity, and AI/Data Science. The default remains custom-only; no IT vocabulary is silently imposed on other classes. Presets require Indonesian input. Other input languages keep the original custom-only workflow.

## Files and behaviour

- `frontend/data/it-courses.json`: one shared UTF-8 catalog for the browser and backend. Ship it with both deployments. Update seeds here, keep IDs stable, and restart the backend after edits (catalog is cached).
- `app/course_profiles.py`: validates selection and merges lecturer overrides into a fresh per-session tuple. Unknown IDs and invalid terms fail before teacher attachment.
- `app/glossary.py`: protects longest exact term matches with XML, avoids substrings, and derives reverse English-to-lecturer pairs. If several sources have the same English translation, the reverse entry is omitted rather than choosing arbitrarily.
- `app/translation_pipeline.py`: applies those terms in lecture and question directions and sends the selected course's short context to DeepL. Original captions are retained by the existing pipeline.
- `frontend/js/course-profiles.js`: preview, early form validation, input-language guard and catalog-load fallback. A catalog-load failure leaves manual glossary available.

This reuses the project's XML `ignore_tags` mechanism and existing API connections. It adds no provider request per term and requires no new API key or model training. It is not a native provider glossary or a trained specialist model. Provider billing and translation quality still need verification; protected text can change grammar, and different meanings may require different entries. Ambiguous short words should be reviewed for the selected lesson.

**Scope:** recognition (Deepgram) and speech pronunciation (ElevenLabs) are unchanged. If the original transcript is wrong or speech is missing in noise, glossary matching cannot reliably reconstruct it. No accuracy percentage or latency improvement is claimed.

## Repeatable classroom check

Use the same recorded audio and same microphone/network conditions with custom-only and the selected preset. Include at least 20 technical terms, paraphrases, negation, numbers and a few unrelated sentences. Record the spoken reference, original transcript, translated text, incorrect terms and listener delay. Repeat with the reverse English question. Have a competent lecturer review meaning as well as terminology. Keep the original audio and participant consent outside Git.

Example prompts (not benchmark results):

- Networks: `Kehilangan paket meningkat saat terjadi kemacetan jaringan.`
- Programming: `Perulangan ini mengubah nilai balik fungsi.`
- Databases: `Kunci asing menjaga integritas referensial.`
- Software: `Pengujian regresi diperlukan setelah integrasi berkelanjutan.`
- Security: `Autentikasi berbeda dengan otorisasi.`
- AI/Data: `Jaringan saraf menggunakan data latih dan data validasi.`
- Reverse: `Why does packet loss increase?`

## Offline checks (no keys)

```powershell
python -m unittest tools.test_course_profiles tools.test_classroom_streaming tools.test_tts_http tools.test_latency_regressions -v
```

Frontend component check (Node.js and jsdom required):

```text
node frontend/tests/course-profiles.js
```

If jsdom is installed outside this repository, set `NODE_PATH` to that dependency directory. Database integration and live provider accuracy are separate tests.
