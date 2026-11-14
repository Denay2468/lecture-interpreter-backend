# Department and course presets

Historical UI documentation: the lecturer preset controls were removed in the background-context update. See [background-context.md](background-context.md). Catalog data and legacy API support remain internal.

Choose a department, then a course in lecturer section 3. Terms and subject context apply automatically when the next lecture starts; viewing terminology is optional. These are starter categories, not an official university curriculum. Presets currently support Indonesian input with English translation. PDF context remains available for other input languages.

| Department | Starter courses |
|---|---|
| Teknologi Informasi / Informatika | Jaringan Komputer / Computer Networks, Pemrograman / Programming, Basis Data / Databases, Rekayasa Perangkat Lunak / Software Engineering, Keamanan Siber / Cybersecurity, AI dan Data / AI and Data Science |
| Kedokteran | Anatomi dan Histologi, Fisiologi |
| Hukum | Hukum Perdata, Hukum Pidana |
| Manajemen | Manajemen Pemasaran, Manajemen Operasi |
| Akuntansi | Akuntansi Keuangan, Pengauditan |
| Teknik Sipil | Analisis Struktur, Mekanika Tanah |
| Teknik Elektro | Rangkaian Listrik, Sistem Kendali |
| Ilmu Komunikasi | Teori Komunikasi, Hubungan Masyarakat |

Switching department clears the previous course but preserves custom terms and uploaded PDFs. Clear or replace old modules when changing the lesson. Custom terms override the selected course. With a course selected, automatic PDF catalog term matching is disabled to avoid unrelated subjects leaking into the glossary; relevant PDF passages still supply translation context. Without a course, existing IT PDF auto-matching remains available.

General / PDF context only supports subjects not yet listed. The catalog is frontend/data/it-courses.json (the historical filename is retained for compatibility). Each course has a unique stable id, department id, subject context, and bilingual starter terms. Do not claim zero translation errors or institution-approved terminology.

Validation:

```sh
python -m unittest tools.test_course_profiles tools.test_materials tools.test_translation_accuracy -v
node frontend/tests/course-profiles.js
```

The JavaScript test requires jsdom.
