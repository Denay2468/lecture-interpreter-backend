# BLB web frontend

Static pages (no build step, no dependencies) for the whole product: sign-in, school admin dashboard, lecturer, student.

| Page | Who | What it does |
|---|---|---|
| `login.html` | everyone | sign in; student sign-up with a university email; enter the emailed code and choose a password (lecturer invitations too); forgot password |
| `admin.html` | school admin | monthly usage, invite/deactivate lecturers, rooms and join codes, recent lectures, email domains (read-only) |
| `teacher.html` | lecturer, admin | pick or create a room (and see its join code), start a lecture, stream the microphone, hear student questions |
| `student.html` | student | join with a room code, live captions and translated audio, push-to-talk questions |

## Run it

1. Start the backend: `python run_server.py` (WebSocket on 8765, HTTP API on 8000).
2. Serve this folder: `cd frontend` then `python -m http.server 8080`.
3. Open http://localhost:8080 and sign in.

Use `localhost` or https: browsers only allow microphone access on a secure origin. The pages call the API at `http://<this host>:8000` and the WebSocket at `ws://<this host>:8765`; change either under **Advanced**. The API only answers pages listed in `CORS_ORIGINS` (default `http://localhost:8080`).

For local development, `python tools/db_setup.py --seed-demo` creates a demo school with an admin, a lecturer and a student (passwords are printed). With `EMAIL_BACKEND=console` the verification codes appear in the server log.

## How people get in

- **School admin:** the operator runs `tools/manage.py create-school`; the admin gets a code, opens *I have a code*, and chooses a password.
- **Lecturer:** the admin invites them on `admin.html`; they get a code, open *I have a code*, and choose a password.
- **Student:** *New student* tab, with their university email. A code is emailed; they enter it and choose a password.

After signing in, each person lands on their own page. A page for a different role sends you back to sign in.

## What the pages exchange with the server

| Direction | Message |
|---|---|
| student -> server (`/ws/student?code=...&token=...`) | `{"type":"ask_press"}`, binary audio frames, `{"type":"ask_release"}` |
| server -> student | `{"type":"mode_changed","mode":"lecture"\|"question"}` (also sent on connect) |
| server -> student | `{"type":"segment","sequence":N,"source_transcript":"...","translated_text":"..."}`, immediately followed by the audio frame with the same sequence |
| server -> student | binary `[uint32 sequence][MP3]`; `question_delivered`; `question_failed` |
| lecturer -> server (`/ws/teacher?room=...&token=...`) | binary audio frames |
| server -> lecturer | `mode_changed`; binary MP3 of a student's question |

Audio frames are `[uint32 sequence][uint32 capture ms]` (big-endian) followed by 20 ms of 16 kHz mono 16-bit little-endian PCM, the same format as `app/protocol.py`. The page resamples the microphone to that in an AudioWorklet. Access tokens last 15 minutes; `api.js` refreshes them and fetches a fresh one on every WebSocket (re)connect.

Close codes shown to people: 4401 session ended (back to sign in), 4403 not allowed (wrong school, subscription or monthly limit), 4404 unknown room or code, 4409 room busy or a student already connected, 4412 no lecture yet (the student page retries), 4413 lecture ended.

## Single-session mode

A backend running without `DATABASE_URL` has no accounts. Open `student.html?legacy=1` or `teacher.html?legacy=1` to use it without signing in.

## Tests

```
node frontend/tests/run.js                                # wire format, resampler, push-to-talk, audio queue, API client (no install)
npm install jsdom && node frontend/tests/dom_smoke.js     # loads every page with a fake server, microphone and audio
```

## Notes

- One student per room for now.
- The session lives in `sessionStorage` (this tab only). Nothing else is stored except the optional server and API addresses (localStorage).
- See `docs/ACCOUNTS.md` for the security model and its known limits.
