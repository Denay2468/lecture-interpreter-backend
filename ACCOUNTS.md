# Accounts, roles and sign-in

## Who exists

```
Platform operator (you)         creates a school, registers its email domains, invites its admin   -> tools/manage.py
  School admin (exactly one)    invites and manages lecturers; sees usage, rooms, recent lectures   -> admin.html
    Lecturers                   create rooms, start lectures                                          -> teacher.html
  Students (self-register)      join a room at their own school with its code                        -> student.html
```

- **One admin per school**, enforced by the database (a unique index), not just by the API. Admins can also teach.
- **Lecturers are invited by their admin** and choose their own password. The admin never sees it.
- **Students register themselves**, but only with an address on one of their school's registered student domains.
- A student can only join rooms at **their own school**. Every admin/lecturer query is scoped to the caller's school; asking about another school's record answers 404.

## What "university email" means here

Two separate checks, because the first proves nothing on its own:

1. **Domain check.** Each school has a list of email domains the operator registered for it (`institution_email_domains`), each marked as staff, students, or both. A domain belongs to exactly one school, so an address identifies its school. This is enforced by a database trigger, so no code path can create an account on an unregistered domain. It is also re-checked on every request: remove a domain and those accounts are locked out at once. Domains are matched **exactly** (`student.umy.ac.id` is not covered by `umy.ac.id`; register each one).
2. **Mailbox ownership.** Anyone can *type* `x@umy.ac.id`. So no account gets a password until its owner enters a 6-digit one-time code emailed to that address (15 minutes, 5 guesses, single use, stored only as an HMAC). This is the same step for a lecturer's invitation and a student's sign-up, and because the password is chosen *at that step*, nobody can pre-register a victim's address with their own password.

With `EMAIL_BACKEND=console` the codes are printed in the server log instead of emailed. That is for development only: **nothing is verified by a real mailbox until you configure SMTP** (`EMAIL_BACKEND=smtp`, `SMTP_*` in `.env`). The stronger production option is single sign-on with the school's Google Workspace or Microsoft tenant, which removes both passwords and email delivery. Not built yet.

## Sessions

- **Access token:** a 15-minute JWT (HS256) with `sub`, `role`, `inst`, `typ=access`. Used as `Authorization: Bearer ...` on the API and as `?token=` on WebSocket URLs.
- **Refresh token:** an opaque random string, stored only as a SHA-256 hash. Each use rotates it. Presenting an already-used one means it leaked, so the whole sign-in is revoked. Logout, a password reset, and deactivating an account all revoke refresh tokens.
- The role in the token is a convenience. **Every request re-reads the account**: active, email verified, domain still registered, role unchanged. Deactivating a lecturer takes effect on their next request, not in 15 minutes.
- Passwords: argon2id, 10 to 128 characters. Sign-in is rate limited per address and per IP; unknown addresses and wrong passwords get the same answer in the same time.

## HTTP API (default port 8000, interactive docs at /docs)

| Route | Who | |
|---|---|---|
| `POST /auth/register` {email, full_name} | anyone | starts a student sign-up; always answers the same way |
| `POST /auth/activate` {email, code, password} | anyone with a code | sets the first password, signs in |
| `POST /auth/login`, `/auth/refresh`, `/auth/logout` | | |
| `POST /auth/password-reset/request`, `/confirm` | anyone | |
| `GET /me` | any signed-in | |
| `GET/POST /admin/lecturers`, `PATCH /admin/lecturers/{id}`, `POST .../resend-invite` | admin | |
| `GET /admin/usage`, `/admin/sessions`, `/admin/email-domains` | admin | |
| `GET/POST /rooms`, `PATCH /rooms/{id}`, `POST /rooms/{id}/rotate-code` | lecturer, admin | a lecturer changes only rooms they own; the admin any in the school |
| `GET /join/{code}` | student | checks a code is at the student's school |

## WebSocket (default port 8765)

```
/ws/teacher?room=<label>&token=<access token>          lecturer or admin; room is in their own school
/ws/student?code=<join code>&token=<access token>      student; the room must be at their own school
```

Close codes: 4401 not signed in or session no longer valid, 4403 wrong role / wrong school / subscription or monthly limit, 4404 unknown room or code, 4409 room busy or a student already connected, 4412 no lecture live yet, 4413 lecture ended. Who you are is settled before anything about the room is revealed.

## Operator runbook

```
python tools/manage.py create-school --name "Universitas X" --domain x.ac.id --admin-email rector@x.ac.id --admin-name "A. Rector"
python tools/manage.py add-domain --school x.ac.id --domain student.x.ac.id --students-only
python tools/manage.py add-domain --school x.ac.id --domain staff.x.ac.id --staff-only
python tools/manage.py resend-admin-invite --admin-email rector@x.ac.id
python tools/db_setup.py --seed-demo      # local development: demo school with an admin, lecturer, student and room
```

Check that a school really controls a domain before registering it: a domain you add is a domain whose every address can sign up. Domains are operator-only on purpose; a school admin who could add `gmail.com` would let anyone in.

## Known limits

- The one admin per school is a single point of failure. Recovery is "forgot password" (needs a working mailbox) or `resend-admin-invite` before they activate.
- Rate limits are in memory: they reset on restart and aren't shared between server processes.
- The frontend keeps the session in `sessionStorage` (this tab only) and the access token appears in WebSocket URLs, so use `wss://`/`https://` outside local testing and restrict `CORS_ORIGINS`. Refresh tokens in JavaScript-readable storage are exposed to any XSS; the pages insert all server text with `textContent`, but httpOnly cookies would be stronger.
- Two tabs refreshing the same token at the same moment can sign the user out (reuse detection can't tell it from theft). The pages share one in-flight refresh per tab.
- One student connection per room.
