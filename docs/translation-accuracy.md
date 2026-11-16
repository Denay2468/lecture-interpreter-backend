# Translation accuracy update

Short chunks need context. Each lecturer segment now carries a snapshot of up
to 1,500 characters of preceding finalized source speech to DeepL's `context`
parameter, alongside the selected course context. Only the current segment is
translated and spoken. Snapshots are captured at enqueue time so concurrent
workers and retries cannot accidentally use later speech. History is scoped
to a lecture queue, is cleared on shutdown, and is not reused for student
questions or another classroom.

Early finalization and size-based cuts now wait when a phrase ends in a
negation/connector (including Indonesian slang) or a partial glossary term,
for example `jaringan` before `saraf`. Real pauses and existing hard limits
still flush, so this does not indefinitely withhold speech. This is a heuristic,
not a grammar model: a pause mid-term can still split it. Waiting for a useful
phrase may add delay; 2–5 words of listener lag is not guaranteed.

Common informal Indonesian IT verbs are normalized before translation.
Explicit glossary overrides still take precedence. Acronyms and backtick code
spans are preserved during slang normalization. Original captions are not
rewritten. English student questions keep the existing reverse glossary; they
do not inherit another student's question history.

No new API, key, package, or .env setting is required. Restart the backend.
Select the appropriate IT course on the lecturer page and add exact technical
term overrides where needed.

## Verification

Offline regressions check source order under two workers, retry consistency,
classroom isolation, bounded history, language routing, glossary protection,
and negation/term boundaries. These tests prove code behavior, not linguistic
accuracy of live Deepgram/DeepL output.

Run:

```powershell
python -m unittest tools.test_translation_accuracy tools.test_course_profiles tools.test_classroom_streaming tools.test_phrase_finalize -v
```

For a measured comparison, use the same recording before and after this update:
20 Indonesian IT sentences, including negation, slang, code-switching and terms
such as `jaringan saraf`, `kunci primer`, and `kehilangan paket`. Repeat in quiet
and cafe noise. Record expected speech, actual source caption, translation,
technical-term errors and meaning/negation errors separately. Have a bilingual
reviewer score the outputs without knowing which version generated them; also
measure median/p95 time from phrase end to the first audible translated sample.
Do not report an accuracy percentage or latency improvement without these runs.

If the source caption is already wrong, capture/microphone placement and STT
need investigation: this change does not recover speech hidden by background
noise or replace the recognition model. If the source is correct but translation
is wrong, save that example with the selected course and glossary for regression
testing. PDF ingestion is a separate feature and is not included here.

Provider reference: https://github.com/DeepL/api-docs/blob/main/docs/customize/overview.mdx
