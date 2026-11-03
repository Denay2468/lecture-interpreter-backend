# Testing streaming classrooms

This change targets the `Hugo-commit` branch. It includes the earlier TTS HTTP
connection-reuse patch, five lecturer languages, classroom fan-out, moderated
questions, an editable glossary, Indonesian colloquial normalization, and
incremental PCM audio playback. Students speak/listen in English.

## Run on Windows

Stop the old backend with Ctrl+C first. Keep PostgreSQL running as a service.
Run **two terminals**, from the repository root:

Terminal 1:
```powershell
python -m pip install -r requirements.txt
python run_server.py
```

Terminal 2:
```powershell
python -m http.server 8080 --directory frontend
```

Open `http://localhost:8080/teacher.html` and log in as lecturer. Open
`http://localhost:8080/student.html` in separate profiles/tabs and log in as
students. Hard-refresh both pages (Ctrl+F5) to load the new protocol/player.
Use `ws://localhost:8766` under Advanced if `.env` has `PORT=8766`; the API
remains on 8000. Run only one backend process.

`AUTO_MIGRATE=true` applies migration 004, which allows English-to-English
lectures. English bypasses MT but still uses STT and TTS. Indonesian, Mandarin
Chinese, Japanese and Spanish are routed to English; questions are routed back
to the lecturer's selected language. Voice quality and provider language/model
support must be checked with the configured account.

## Manual acceptance test

1. Choose Indonesian and enter a glossary, one pair per line:
   ```text
   tektokan = back-and-forth conversation
   spanning tree = Spanning Tree
   jaringan komputer = computer network
   ```
2. Start the lecture. Join the same room with two student browsers. Both should
   receive identical captions and audio in order. Each has an independent bounded
   delivery queue; a slow connection cannot take another student's segments.
3. Speak continuously: “Gua nggak ngerti gimana jaringan komputer bekerja.
   Sekarang kita bahas spanning tree. Kita bisa tektokan setelah penjelasan.”
   The displayed source remains unchanged. Colloquial tokens are normalized only
   for translation; exact glossary phrases have priority. Check meaning, not
   just whether some audio is produced.
4. Students hear completed stable phrases while the lecturer continues speaking.
   Streaming uses finalized STT results (six words or 32 characters), not mutable
   interim guesses. Endpointed shorter utterances still work. STT may not finalize
   promptly, so this is **not a guaranteed 500 ms or 1.5 s system**.
5. Click Raise hand, enable the microphone, and approve the student on the teacher
   page. Only that connection can send question audio. Hold to ask, then release.
   Grants/held microphones expire after 45 seconds. Other students' disconnects
   must not end the active question. The next grant waits for question processing.
6. Switch off student sound: captions should continue. Re-enable sound and use
   Enable sound if the browser blocks autoplay. Use headphones to avoid feedback.
7. In a separate test, use an invalid TTS key and restart: captions should remain,
   with an audio failure message. Restore the real key afterwards. Never publish
   keys or `.env`. An interrupted PCM stream is not retried after partial playback,
   avoiding repeated words. The next segment must still play.
8. Compare with `STREAMING_AUDIO_ENABLED=false` and
   `INCREMENTAL_PHRASES_ENABLED=false`, restarting between runs. Use the same
   sentences and network. `SLANG_NORMALIZATION_ENABLED=false` disables built-ins
   for a separate quality comparison. Clear the course glossary for a baseline.

The join link and QR are generated locally, without sending the room code to a
QR service. A localhost QR only works on the same computer. For phones use a
reachable HTTPS frontend and secure WebSocket deployment; microphone access on
plain HTTP LAN addresses may be blocked. The QR carries the room code and WS
port, not an access token. Students still sign in. The code survives login.

## Metrics and limits

The teacher's Session measurements show successful/failed **server** segments,
submitted MT/TTS character attempts, and p50/p95 of the last 500 browser playback
reports. Silent segments do not count as failed. Successful provider delivery is
not proof every student's speaker played it; the report sample count is separate.

`segment_ready_to_playback_ack` starts when the finalized phrase enters the
translation queue and ends when the server receives the browser's first playback
report. It includes queueing, MT, first TTS audio, network/browser scheduling, and
report return time. It **excludes capture, jitter and STT finalization** and is not
end-to-end speech latency. Measure true end-to-end using a recording of source
speech and student output, and report the reference point (phrase end → first
translated sound). A 5-second segment duration is not automatically 5-second lag.

Costs remain unknown until all three optional rates are configured. The estimate
uses lecture wall time as approximate STT time and submitted MT/TTS characters,
including retries and questions through the shared client. It excludes extra
question STT, hosting, tax, free tiers and provider billing rounding. Hourly cost
is a projection, not an invoice. Audio fan-out does not synthesize separately for
each student.

Audio uses signed little-endian 16-bit PCM at 24 kHz in base64 JSON chunks over
WebSocket; browser Web Audio schedules them in order. This trades more bandwidth
(~64 KB/s per listening student before WebSocket overhead) for progressive
playback. Outgoing queues are bounded; a browser more than eight seconds behind
is told to reconnect rather than silently accumulating minutes of stale speech.
Mid-segment joins start at the next segment to avoid playing a fragment.

The course glossary is an exact phrase mapping (40 pairs/8000 characters max),
protected with DeepL XML ignore tags; it is not DeepL's managed glossary service.
It does not fix an STT misrecognition or infer ambiguous slang/sarcasm. Avoid
mapping ambiguous words such as “buat” globally. Short phrases can sacrifice
context/intonation; evaluate accuracy with real lecture examples.

## Automated checks

```powershell
python -m unittest tools.test_classroom_streaming tools.test_tts_http tools.test_latency_regressions -v
python tools/test_rule5_playback_pause.py
python tools/test_segmenter_logic.py
python tools/test_translation_queue_logic.py
python tools/test_question_streaming_logic.py
node frontend/tests/run.js
node frontend/tests/pcm-player.js
```

For DOM integration tests install jsdom in a development environment, then run
`node frontend/tests/dom_smoke.js`. These tests use fake providers/audio. They do
not claim production latency or translation accuracy. Database integration tests
require `TEST_DATABASE_URL` pointing at a disposable test database.

The previously added clause streamer was inserted into a module docstring and
replayed interim hypotheses; it has been integrated into the existing Segmenter
using stable final chunks instead. Telemetry is session-scoped, and the wrongly
named `app/ytelemetry.p` is replaced by importable `app/telemetry.py` helpers.
