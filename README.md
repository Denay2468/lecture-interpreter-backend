# Lecture Interpreter - Backend (MVP core)

A real-time, two-way interpreter backend for multilingual classrooms. A teacher speaks Indonesian and the student hears English, continuously. The student can hold Ask to put a question to the teacher, which is translated back into Indonesian.

```
Teacher audio -> Jitter Buffer -> Hybrid Segmentation -> Translation Queue (MT -> TTS) -> Ordered Student Playback
Student question (hold Ask) -> streamed STT -> MT -> TTS -> teacher
```

**Status.** The lecture direction and Question Mode have both been run end to end against real Deepgram, DeepL and ElevenLabs. Streaming question STT is the newest piece: its orchestration is tested with fakes, but it has not had its first live run yet. Every other piece of logic has local tests (`tools/test_*.py`) that need no network or API keys.

**Not in the MVP.** Real browser or mobile clients (everything is exercised with the scripts in `tools/`), multiple sessions or students, persistence.

## How Segmentation works

`app/segmentation.py` implements the "hybrid trigger" from the architecture spec, mapped onto Deepgram's actual streaming API:

| Spec concept | Concrete implementation |
|---|---|
| Primary trigger: "STT completion signal" | Deepgram's `speech_final` flag on a Results message (endpointing-informed, not just raw silence) |
| Fallback/backstop: "VAD silence" | Deepgram's `UtteranceEnd` event (a separate, longer silence timer Deepgram itself describes as a safety net) |
| *(added here, not in the original spec)* | A hard max-segment-duration cap, so a segment can never grow unboundedly if neither signal fires |

Unit-tested without any network call — see `tools/test_segmenter_logic.py`.

## How the Translation Queue + Pipeline works

`app/translation_pipeline.py` implements the queue model and failure handling from architecture spec Sections 4 and 7:

- **Strict FIFO, no eviction by age** (Rule 6).
- **Retry once, then skip and continue** (Rule 7) — a segment that fails MT or TTS twice is marked `skipped` rather than blocking everything behind it.
- **Every segment always produces exactly one result** — including silence/noise segments, which emit `state="skipped", error=None` rather than nothing at all. This was a deliberate design change made *while building* Ordered Playback: a reorder buffer waiting for "segment #3" needs to know #3 is definitely coming, or it has no way to distinguish "still processing" from "will never arrive." Making every segment deterministic avoids that whole class of ambiguity.
- STT already happened during Segmentation, so this stage is just MT (DeepL) → TTS (ElevenLabs).

Unit-tested with fake MT/TTS functions — see `tools/test_translation_queue_logic.py`.

## How Ordered Student Playback works

`app/playback.py` solves a real problem the Translation Queue's own concurrency creates: with multiple workers, segment #2 can finish translating before segment #1 (e.g. a slower API response). `StudentPlaybackController` buffers out-of-order results and releases them strictly in original speaking order — similar in spirit to the Jitter Buffer's reordering, but simpler: because every segment is now guaranteed to eventually produce a result, this buffer never needs a give-up-and-move-on timeout the way the Jitter Buffer does for genuinely-lost network packets. It can just always wait for the next sequence number.

Unit-tested by deliberately submitting results out of order and verifying they release correctly — see `tools/test_playback_ordering.py`.

## How Question Mode works

`app/mode_state.py` (pure, unit-tested) + `app/session.py` (the real wiring) implement the 13 product rules established earlier in this project:

| Rule | Where it's implemented |
|---|---|
| 1 — single active speaker | `ModeStateMachine`: exactly one of `lecture`/`question` at any time |
| 2 — instant, synchronous switch | `press()`/`release()` flip the mode immediately, no waiting |
| 3 — in-flight teacher audio never discarded | Comes almost for free: gating happens at *frame intake*, so anything already in the jitter buffer/segmentation/translation queue keeps processing untouched. The one explicit step: `Segmenter.force_close()` finalizes whatever segment was mid-flight the instant a question interrupts it, rather than waiting on a silence timeout that will never come |
| 4 — teacher not captured during Question Mode | `SessionCoordinator.handle_teacher_frame` drops frames before they reach `TeacherAudioIngest` |
| 5 — playback pauses, not processing | The Translation Queue's workers have zero knowledge of mode and never stop; only `/ws/student`'s send loop pauses dequeuing, via `lecture_mode_event` |
| 6 — no segment dropped as "old" | Unaffected — the pre-existing FIFO guarantee already covers this |
| 7 / 8 — retry-then-skip vs retry-then-visible-failure | Lecture segments: Rule 7 (unchanged, `translation_pipeline.py`). Questions: Rule 8 — a failed question sends the student a `question_failed` control message instead of failing silently |
| 11 — mic state visible to both | `_broadcast_mode` sends `mode_changed` to both teacher and student connections on every transition |
| 12 — debounce short presses | `ModeStateMachine.release()` reports whether the press was long enough; a debounced press's captured audio is discarded before ever reaching the Question Pipeline |
| 13 — mic stays live for the whole press | No silence/VAD logic on this path at all — frames are accepted for as long as mode stays Question, full stop |

Rules 9 and 10 (no silence timeout needed; pre/post-roll buffering) are primarily **client-side** concerns per the architecture spec — a real client keeps a local circular buffer that's never transmitted until Ask is pressed. This backend doesn't implement that buffer (there's no real client yet); `tools/receive_student_audio.py` simulates a *rough* approximation of it with `--post-roll-ms`, but that's a test-harness stand-in, not the real thing.

**The question pipeline itself** (`app/question_pipeline.py`) is simpler than the lecture pipeline: there's no streaming segmentation involved, since the "segment" boundary is just the press-to-release window. One clip in, STT (English) → MT (English→Indonesian) → TTS (Indonesian) → delivered whole to the teacher.

**Streaming question STT.** The question's audio is forwarded to Deepgram while the student is still speaking (`QuestionTranscriber` in `app/question_pipeline.py`) instead of after release. Opening a Deepgram connection took 3-5s in testing and used to happen after release, which made it most of the 5-6s STT step. If the streaming path fails for any reason, the pipeline falls back to the old one-shot call on the audio `session.py` buffers in parallel. Look for the log line `question STT streamed: connected X s after press`.

### Testing Question Mode

You'll need a short English question clip (16kHz mono PCM16 WAV — same conversion steps as every other test clip in this project). Then, same three-terminal setup as Flow A, but terminal 2 adds two flags:

```bash
# Terminal 1
python run_server.py

# Terminal 2 — now also simulates pressing Ask
python tools/receive_student_audio.py --question-audio sample_audio/my_question.wav --ask-after-segments 2

# Terminal 3
python tools/send_test_audio.py sample_audio/lecture_sample_id_v2.wav
```

What to watch for across all three terminals:
- Terminal 1 logs `Question Mode entered` ~10s in, a `force_close` on whatever segment was mid-flight, then the question's STT/MT/TTS, then `Question Mode exited`.
- Terminal 2 prints `[mode] >>> QUESTION <<<`, then later `[question] Delivered: ... -> ...` and `[mode] >>> LECTURE <<<`. Lecture segments should visibly stop arriving during the question and resume (in order) right after.
- Terminal 3 (teacher) prints `[received] Question answer audio ... -> saved as question_answer_001.mp3` — that's the synthesized Indonesian answer. Listen to it.

## Project structure

```
lecture-interpreter-backend/
  run_server.py                  entry point
  requirements.txt
  .env.example
  app/
    config.py                    all settings
    protocol.py                  wire formats (teacher->server, server->student)
    jitter_buffer.py             reorders and paces teacher audio frames
    teacher_ingest.py            TeacherAudioIngest: one jitter buffer per session
    segmentation.py              hybrid STT-completion + VAD segmentation
    translation_pipeline.py      FIFO queue, retry-once-then-skip, MT -> TTS
    playback.py                  reorders translated segments into speaking order
    mode_state.py                Lecture <-> Question state + short-press debounce
    question_pipeline.py         student question: streamed STT -> MT -> TTS
    session.py                   SessionCoordinator: wires it all, enforces the rules
    server.py                    WebSocket server (/ws/teacher, /ws/student)
  tools/
    send_test_audio.py           teacher test client
    receive_student_audio.py     student test client (can simulate holding Ask)
    test_*.py                    local tests, no API keys needed
```

## 1. Install

```bash
python -m venv venv
venv\Scripts\activate        # Windows; use `source venv/bin/activate` on Mac/Linux
pip install -r requirements.txt
```

## 2. Verify all the logic locally first (no API keys needed)

```bash
# every test_*.py in tools/ (PowerShell):
Get-ChildItem tools/test_*.py | ForEach-Object { python $_.FullName }
```

These feed fake events/functions/results directly into each piece of logic — segment boundaries, retry/skip behavior, and (critically) reordering — all without touching the network.

## 3. Add your API keys

```bash
cp .env.example .env
```

Fill in `DEEPGRAM_API_KEY`, `DEEPL_API_KEY`, `ELEVENLABS_API_KEY`, and `ELEVENLABS_VOICE_ID` — the same keys from the benchmark project all work here.

## 4. Run the full end-to-end test

You'll want **three terminals** now:

```bash
# Terminal 1 — the server
python run_server.py

# Terminal 2 — the "student" listening for translated audio
python tools/receive_student_audio.py

# Terminal 3 — the "teacher" streaming Indonesian audio
python tools/send_test_audio.py sample_audio/lecture_sample_id_short.wav
```

Order doesn't matter much between terminals 1 and 2 — the student connection waits for a teacher session to become active. Terminal 3 is what actually kicks things off.

Watch terminal 2 — you should see segments arrive and get saved to `received_audio/`, with an order check at the end confirming `arrival order == sequence order`. That final check is the actual proof this component does its job.

### Testing under bad network conditions too

Combine with the jitter/loss simulation from earlier build phases:
```bash
python tools/send_test_audio.py sample_audio/lecture_sample_id_short.wav --jitter-ms 80 --drop-rate 0.03
```

## Multi-tenant mode (PostgreSQL)

Set `DATABASE_URL` and `JWT_SECRET` in `.env` and every lecture becomes a row in PostgreSQL: institutions have rooms, lecturers start lectures in them, and usage is metered against the institution's monthly audio limit. Leave `DATABASE_URL` unset and the server runs in single-session mode exactly as before (no accounts, rooms or metering), which is what the local test scripts use.

```
python tools/db_setup.py --seed-demo     # applies migrations, creates a demo room, prints a join code and a dev token
python run_server.py                     # applies pending migrations too (AUTO_MIGRATE=false to skip)
```

**Starting a lecture.** The lecturer connects to `/ws/teacher?room=<label>&token=<JWT>`. The server checks the token, looks the user up (active? which institution?), finds the room in *that* institution, and starts the session in the database in one transaction: subscription status, monthly quota, and one-live-lecture-per-room are all checked there. Only then is the speech pipeline brought up, so a refused lecture never opens a Deepgram connection. The pair is `id -> en`.

**Joining.** Students sign in with their university email and connect to `/ws/student?code=<join code>&token=<access token>`; the room must be at their own school and have a live lecture.

**Metering.** Billable time is wall-clock seconds from the session row being created to the lecturer's connection ending, measured by the database clock and written once, atomically, when the lecture ends. Usage for the month is the sum of those durations plus the elapsed time of any running lecture, in the institution's timezone. The limit is checked when a lecture starts; a running lecture is never cut off.

**Heartbeats and crashes.** While a lecture runs the server stamps `last_heartbeat_at` every 30s. A session whose heartbeat stops for 90s is marked `ABORTED` and billed up to its last heartbeat, so a crash neither loses the usage nor bills the downtime. On startup, sessions a previous process left `LIVE` are settled the same way.

**The database is never in the audio path.** Starting a lecture fails closed (no database, no quota check, no lecture). Once a lecture is running, a failing heartbeat or completion is logged and settled later; the translation pipeline is unaffected.

| Close code | Meaning |
|---|---|
| 4400 | bad request (unsupported language pair) |
| 4401 | invalid or expired token, or unknown/deactivated user |
| 4403 | forbidden: subscription not active, monthly limit reached, lecturer not allowed |
| 4404 | unknown room label or join code |
| 4409 | room already has a live lecture / a student is already connected |
| 4412 | valid code, but no lecture is live yet (client should retry) |
| 4413 | the lecture ended |
| 1011 | server error (database unavailable, speech pipeline failed to start) |

Test clients: `send_test_audio.py --room DEMO-101 --token <lecturer token>` and `receive_student_audio.py --code <join code> --token <student token>` (`python tools/db_setup.py --seed-demo` prints ready-to-run commands).

**Known limits.** One student connection per room (translated audio goes to a single playback queue), one server instance (live sessions are held in memory), and sign-in uses the built-in accounts system (see below).

## Accounts, roles and the HTTP API

One admin per school, who invites lecturers; students self-register. Everyone must use an email address on a domain their school registered, and must prove they own the mailbox with a one-time code before they get a password. A FastAPI app on port 8000 (docs at `/docs`) handles sign-up, sign-in, the admin dashboard and rooms; the WebSocket handshakes enforce the same role and school checks. Setup for a school is an operator task: `python tools/manage.py create-school ...`. Full details, the security model and its limits are in `docs/ACCOUNTS.md`.

## Web frontend

`frontend/` holds a student page and a lecturer page (plain HTML/JS, no build step). Run `python -m http.server 8080` inside it and open http://localhost:8080. See `frontend/README.md`. Students now also receive each translated segment as text (`{"type":"segment",...}`, sent just before its audio) so the page can show captions.

## What's next

- **First live run of streaming question STT.** Check terminal 1 for `question STT streamed: connected X s after press, N bytes fed live, Y s from release to transcript`, and compare the `question pipeline timings` STT figure with the 5.1-6.2s it was before.
- **Interrupted lecture segments.** When Ask lands mid-sentence, that segment closes with no transcript yet, because Deepgram finalizes text seconds after the speech. The text is not lost (it carries over), but it gets glued onto the next segment: one live run produced an 11.2s merged segment delivered late. A likely fix is to feed Deepgram a little silence on interruption so pending words finalize before the segment closes.
- **Streaming TTS playback.** Translated audio is sent only after the whole clip is synthesized.
- **Session lifecycle.** One session at a time; a student connected when the teacher session ends or restarts is not re-attached.
- **Real clients,** which have to implement the wire framing in `protocol.py`.

## A note on the wire protocol

`protocol.py` defines two placeholder framings: one for teacher→server audio (sequence + timestamp + PCM, used by the Jitter Buffer for reordering) and a simpler one for server→student audio (sequence + complete synthesized clip, since each translated segment is already a self-contained playable unit, not a continuous stream needing frame-level pacing). Neither has been validated against a real browser/mobile client yet — when you build the actual teacher- and student-facing clients, you'll need to implement this same framing there, or revisit the protocol together with that work.

