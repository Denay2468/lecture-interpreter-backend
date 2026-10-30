"use strict";
// Loads the real pages in jsdom with a fake server, WebSocket, audio and microphone and drives them like a user.
// Needs jsdom:  npm install jsdom   then   node frontend/tests/dom_smoke.js
const path = require("path");
const { pathToFileURL } = require("url");
const { JSDOM } = require("jsdom");
const root = path.resolve(__dirname, "..");
let failures = 0;
const check = (c, d) => { console.log("  [" + (c ? "PASS" : "FAIL") + "] " + d); if (!c) failures++; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const text = (n) => n.textContent.replace(/\s+/g, " ").trim();

class FakeWS {
  constructor(url) { this.url = url; this.readyState = 0; this.sent = []; FakeWS.all.push(this); setTimeout(() => { this.readyState = 1; if (this.onopen) this.onopen({}); }, 0); }
  send(d) { this.sent.push(d); }
  close(code, reason) { if (this.readyState === 3) return; this.readyState = 3; this.closedWith = code; setTimeout(() => this.onclose && this.onclose({ code: code || 1000, reason: reason || "" }), 0); }
  receive(d) { this.onmessage({ data: d }); }
  serverClose(code, reason) { this.readyState = 3; this.onclose({ code, reason: reason || "" }); }
}
FakeWS.OPEN = 1; FakeWS.all = [];
class FakeAudio {
  constructor(src) { this.src = src; this.listeners = {}; FakeAudio.all.push(this); }
  addEventListener(t, f) { this.listeners[t] = f; }
  play() { this.played = true; return Promise.resolve(); }
  pause() {}
  emit(t) { if (this.listeners[t]) this.listeners[t](); }
}
FakeAudio.all = [];
class FakeMic {
  constructor(o) { this.o = o; FakeMic.last = this; }
  async start() {}
  stop() { this.stopped = true; }
}
const memStorage = () => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) }; };
const user = (role, name) => ({ id: "u-" + role, role, full_name: name || role, email: role.toLowerCase() + "@demo.example", institution_id: "i1" });
const session = (role, token, name) => JSON.stringify({ access_token: token, refresh_token: "refresh-" + token, expires_at: Date.now() + 600000, user: user(role, name) });
const tokensFor = (role, name) => ({ access_token: "a-" + role, refresh_token: "r-" + role, expires_in: 900, user: user(role, name) });

async function load(page, opts) {
  opts = opts || {};
  FakeWS.all = []; FakeAudio.all = []; FakeMic.last = null;
  const calls = [], redirects = [], routes = opts.routes || {};
  let stores;
  const file = path.join(root, page);
  const dom = await JSDOM.fromFile(file, {
    url: pathToFileURL(file).href + (opts.search || ""),
    runScripts: "dangerously", resources: "usable", pretendToBeVisual: true,
    beforeParse(w) {
      w.WebSocket = FakeWS; w.Audio = FakeAudio;
      w.URL.createObjectURL = () => "blob:fake"; w.URL.revokeObjectURL = () => {};
      w.HTMLElement.prototype.scrollIntoView = function () {};
      const ss = memStorage(), ls = memStorage();
      if (opts.session) ss.setItem("blb.session", opts.session);
      Object.defineProperty(w, "sessionStorage", { value: ss, configurable: true });
      Object.defineProperty(w, "localStorage", { value: ls, configurable: true });
      stores = { ss, ls };
      w.BLB_NAVIGATE = (u) => redirects.push(u);
      w.fetch = async (url, o) => {
        const p = url.replace(/^https?:\/\/[^/]+/, ""), method = (o && o.method) || "GET";
        calls.push({ method, path: p, body: o && o.body ? JSON.parse(o.body) : undefined, auth: o && o.headers && o.headers.Authorization });
        const h = routes[method + " " + p];
        if (!h) return { ok: false, status: 404, json: async () => ({ detail: "no route " + method + " " + p }) };
        const r = typeof h === "function" ? h(calls[calls.length - 1].body) : h;
        const status = r.status || 200;
        return { ok: status < 400, status, json: async () => r.json };
      };
    },
  });
  await new Promise((r) => { if (dom.window.document.readyState === "complete") r(); else dom.window.addEventListener("load", r); });
  const w = dom.window;
  w.BLB.MicStreamer = FakeMic;
  await sleep(40);
  const frame = (seq, bytes) => { const b = new w.ArrayBuffer(4 + bytes.length); new w.DataView(b).setUint32(0, seq, false); new w.Uint8Array(b, 4).set(bytes); return b; };
  return { w, $: (id) => w.document.getElementById(id), frame, last: () => FakeWS.all[FakeWS.all.length - 1], calls, redirects, stores };
}
const strings = (ws) => ws.sent.filter((s) => typeof s === "string");
const binaries = (ws) => ws.sent.filter((s) => typeof s !== "string");
const submit = (w, id) => w.document.getElementById(id).dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
const rowsOf = (w, id) => Array.from(w.document.querySelectorAll("#" + id + " tbody tr")).map((tr) => Array.from(tr.children).map(text));

async function loginPage() {
  console.log("\nlogin page");
  const routes = {
    "POST /auth/login": (b) => (b.password === "right-password-1" ? { json: tokensFor("LECTURER", "Tara") } : { status: 401, json: { detail: "Incorrect email or password." } }),
    "POST /auth/register": (b) => (/@gmail\.com$/.test(b.email) ? { status: 422, json: { detail: "That email domain isn't registered with a school on BLB. Use your official university email." } } : { status: 202, json: { message: "ok" } }),
    "POST /auth/activate": (b) => (b.code === "123456" ? { json: tokensFor("STUDENT", "Kim") } : { status: 400, json: { detail: "That code is wrong or has expired." } }),
    "POST /auth/password-reset/request": { status: 202, json: { message: "ok" } },
    "POST /auth/password-reset/confirm": (b) => (b.code === "654321" ? { json: { message: "done" } } : { status: 400, json: { detail: "That code is wrong or has expired." } }),
  };
  let p = await load("login.html", { routes });
  const visible = () => Array.from(p.w.document.querySelectorAll("[data-panel]")).filter((f) => !f.hidden).map((f) => f.dataset.panel);
  check(JSON.stringify(visible()) === '["signin"]', "the sign-in form is shown first");
  p.$("si-email").value = "tara@demo.example"; p.$("si-password").value = "wrong-password"; submit(p.w, "signin"); await sleep(30);
  check(text(p.$("status")).includes("Incorrect email or password") && p.$("status").className.includes("error") && p.redirects.length === 0, "a wrong password shows the server's message and stays on the page");
  p.$("si-password").value = "right-password-1"; submit(p.w, "signin"); await sleep(30);
  check(p.redirects[0] === "teacher.html" && JSON.parse(p.stores.ss.getItem("blb.session")).access_token === "a-LECTURER", "a lecturer signs in and lands on the lecturer page, with the session stored for this tab");

  p = await load("login.html", { routes });
  p.w.document.querySelector('[data-tab="register"]').click();
  check(JSON.stringify(visible()) === '["register"]', "the 'New student' tab shows the sign-up form");
  p.$("rg-name").value = "Kim Student"; p.$("rg-email").value = "kim@gmail.com"; submit(p.w, "register"); await sleep(30);
  check(text(p.$("status")).includes("official university email") && JSON.stringify(visible()) === '["register"]', "a non-university email is refused with a clear message");
  p.$("rg-email").value = "kim@demo.example"; submit(p.w, "register"); await sleep(30);
  check(JSON.stringify(visible()) === '["activate"]' && p.$("ac-email").value === "kim@demo.example" && text(p.$("status")).includes("6-digit"), "after sign-up the code step opens with the email filled in");
  p.$("ac-code").value = "000000"; p.$("ac-password").value = "a long enough password"; submit(p.w, "activate"); await sleep(30);
  check(text(p.$("status")).includes("wrong or has expired") && p.redirects.length === 0, "a wrong code is refused");
  p.$("ac-code").value = "123456"; submit(p.w, "activate"); await sleep(30);
  check(p.redirects[0] === "student.html", "the right code creates the account and lands on the student page");

  p = await load("login.html", { routes });
  p.w.document.querySelector('[data-tab="reset"]').click();
  p.$("rs-email").value = "tara@demo.example"; submit(p.w, "reset"); await sleep(30);
  check(!p.$("reset-step2").hidden && text(p.$("rs-submit")) === "Change my password", "a reset request reveals the code and new-password fields");
  p.$("rs-code").value = "654321"; p.$("rs-password").value = "another long password"; submit(p.w, "reset"); await sleep(30);
  check(JSON.stringify(visible()) === '["signin"]' && p.$("si-email").value === "tara@demo.example" && text(p.$("status")).includes("Password changed"), "after the reset the sign-in form is back with the email filled in");

  p = await load("login.html", { routes: { "POST /auth/login": () => { throw new Error("network down"); } } });
  p.$("si-email").value = "a@demo.example"; p.$("si-password").value = "x"; submit(p.w, "signin"); await sleep(30);
  check(text(p.$("status")).includes("reach the server"), "an unreachable API is explained");
  p = await load("login.html", { session: session("ADMIN", "adm-tok") });
  check(p.redirects[0] === "admin.html", "someone already signed in is sent straight to their page");
}

async function adminPage() {
  console.log("\nadmin dashboard");
  let lecturers = [
    { id: "l1", full_name: "Tara Teacher", email: "tara@demo.example", is_active: true, email_verified_at: "2026-01-01T00:00:00Z" },
    { id: "l2", full_name: "Ivan Invited", email: "ivan@demo.example", is_active: true, email_verified_at: null },
  ];
  const routes = {
    "GET /me": { json: { institution_name: "Demo University" } },
    "GET /admin/usage": { json: { used_seconds: 5400, limit_seconds: 36000, remaining_seconds: 30600 } },
    "GET /admin/lecturers": () => ({ json: lecturers }),
    "POST /admin/lecturers": (b) => {
      if (/@gmail\.com$/.test(b.email)) return { status: 422, json: { detail: "Lecturers must use one of your school's staff email domains: demo.example" } };
      lecturers = lecturers.concat([{ id: "l3", full_name: b.full_name, email: b.email, is_active: true, email_verified_at: null }]);
      return { status: 201, json: lecturers[lecturers.length - 1] };
    },
    "PATCH /admin/lecturers/l1": (b) => { lecturers = lecturers.map((l) => (l.id === "l1" ? Object.assign({}, l, { is_active: b.is_active }) : l)); return { json: lecturers[0] }; },
    "POST /admin/lecturers/l2/resend-invite": { status: 202, json: { message: "Invite sent." } },
    "GET /rooms": { json: [{ id: "r1", room_code: "CS-302", room_name: "Algorithms", join_code: "K7P2QX", lecturer_name: "Tara Teacher", is_live: true }] },
    "POST /rooms/r1/rotate-code": { json: { join_code: "NEWCODE" } },
    "GET /admin/sessions?limit=25": { json: [{ id: "s1", status: "COMPLETED", started_at: "2026-10-01T08:00:00Z", duration_seconds: 1800, room_code: "CS-302", lecturer_name: "Tara Teacher" }] },
    "GET /admin/email-domains": { json: [{ domain: "demo.example", allow_staff: true, allow_students: true }] },
  };
  const p = await load("admin.html", { session: session("ADMIN", "adm-tok", "Ada Admin"), routes });
  check(text(p.$("school-name")) === "Demo University" && p.$("whoami").textContent === "Ada Admin", "the school and the signed-in admin are shown");
  check(text(p.$("usage-text")).startsWith("1.5 of 10 hours used") && p.$("usage-bar").style.width === "15%", "monthly usage is shown as text and a bar");
  check(p.calls.every((c) => c.auth === "Bearer adm-tok"), "every API call carries the access token");
  let rows = rowsOf(p.w, "lecturers");
  check(rows.length === 2 && rows[0][2] === "Active" && rows[1][2] === "Invited", "lecturers are listed with Active / Invited status");
  check(!rows[0][3].includes("Resend") && rows[1][3].includes("Resend invite"), "only an invited lecturer offers 'Resend invite'");
  Array.from(p.w.document.querySelectorAll("#lecturers button")).find((b) => b.textContent === "Deactivate").click(); await sleep(40);
  check(p.calls.some((c) => c.method === "PATCH" && c.body.is_active === false), "Deactivate sends the change");
  rows = rowsOf(p.w, "lecturers");
  check(rows[0][2] === "Deactivated" && rows[0][3].includes("Reactivate"), "the list refreshes to show them deactivated");
  Array.from(p.w.document.querySelectorAll("#lecturers button")).find((b) => b.textContent === "Resend invite").click(); await sleep(30);
  check(text(p.$("invite-status")).includes("Invite sent to ivan@demo.example"), "resending an invite confirms it");

  p.$("inv-name").value = "Bad One"; p.$("inv-email").value = "bad@gmail.com"; submit(p.w, "invite-form"); await sleep(30);
  check(text(p.$("invite-status")).includes("staff email domains") && p.$("invite-status").className.includes("error"), "inviting a non-staff address shows the server's explanation");
  p.$("inv-name").value = "New Lecturer"; p.$("inv-email").value = "new@demo.example"; submit(p.w, "invite-form"); await sleep(40);
  check(text(p.$("invite-status")).includes("Invite sent to new@demo.example") && rowsOf(p.w, "lecturers").length === 3 && p.$("inv-email").value === "", "a valid invite is sent, the form clears and the list grows");

  const rooms = rowsOf(p.w, "rooms");
  check(rooms[0][0] === "CS-302 - Algorithms" && rooms[0][2] === "K7P2QX" && rooms[0][3] === "Live", "rooms show their join code and whether they are live");
  Array.from(p.w.document.querySelectorAll("#rooms button")).find((b) => b.textContent === "New code").click(); await sleep(30);
  check(p.calls.some((c) => c.method === "POST" && c.path === "/rooms/r1/rotate-code"), "'New code' rotates a room's join code");
  const sess = rowsOf(p.w, "sessions");
  check(sess[0][0] === "CS-302" && sess[0][3] === "30 min" && sess[0][4] === "Completed", "recent lectures show room, length and status");
  check(text(p.$("domains")) === "demo.example (staff and students)", "the school's email domains are listed (read-only)");

  const q = await load("admin.html", { session: session("LECTURER", "lec-tok") });
  check(q.redirects[0] === "login.html" && q.calls.length === 0, "a lecturer is sent away from the admin page without any API call");
  const e = await load("admin.html", { session: session("ADMIN", "adm-tok"), routes: { "GET /me": { status: 401, json: { detail: "expired" } }, "POST /auth/refresh": { status: 401, json: { detail: "expired" } } } });
  check(e.redirects.includes("login.html") && e.stores.ss.getItem("blb.session") === null, "an expired session sends the admin back to sign in");
}

async function studentPage() {
  console.log("\nstudent page");
  let p = await load("student.html");
  check(p.redirects[0] === "login.html", "without signing in, the student page redirects to the login page");
  p = await load("student.html", { session: session("LECTURER", "lec-tok") });
  check(p.redirects[0] === "login.html", "a lecturer's session doesn't open the student page");
  p = await load("student.html", { session: session("STUDENT", "stu-tok", "Kim Student") });
  const { w, $ } = p;
  check($("whoami").textContent === "Kim Student" && !$("signout").hidden, "the student's name and a Sign out button are shown");
  check($("server-url").value === "ws://localhost:8765", "the server address defaults to ws://localhost:8765");
  $("join").click();
  check(text($("status")).includes("Enter the room code"), "joining with no code is refused with a hint");
  $("code").value = "q86-uty"; $("join").click(); await sleep(30);
  check(p.last().url === "ws://localhost:8765/ws/student?code=Q86UTY&token=stu-tok", "the code is cleaned up and sent with the student's token (" + p.last().url.replace(/token=.*/, "token=***") + ")");
  const ws = p.last();
  ws.receive(JSON.stringify({ type: "mode_changed", mode: "lecture" }));
  check(!$("live-card").hidden && text($("state-pill")) === "Live", "the first mode message opens the live view");
  ws.receive(JSON.stringify({ type: "segment", sequence: 0, source_transcript: "Selamat pagi", translated_text: "Good morning <b>everyone</b>" }));
  const caps = w.document.querySelectorAll(".caption");
  check(caps.length === 1 && text(caps[0]).includes("Good morning") && text(caps[0]).includes("Selamat pagi") && caps[0].querySelector("b") === null, "a caption shows the English and the Indonesian, inserted as text, never as HTML");
  ws.receive(p.frame(0, [1, 2, 3])); await sleep(10);
  check(FakeAudio.all.length === 1 && FakeAudio.all[0].played && caps[0].classList.contains("playing"), "audio plays and its caption is highlighted");
  FakeAudio.all[0].emit("ended"); await sleep(10);
  check(!caps[0].classList.contains("playing"), "the highlight clears when the audio ends");
  $("sound").checked = false; $("sound").dispatchEvent(new w.Event("change"));
  ws.receive(p.frame(1, [4, 5])); await sleep(10);
  check(FakeAudio.all.length === 1, "with sound off, audio isn't played (captions still work)");
  $("sound").checked = true;

  $("enable-mic").click(); await sleep(10);
  check(!$("ask").disabled && $("enable-mic").hidden, "enabling the microphone enables the Ask button");
  for (let i = 0; i < 5; i++) FakeMic.last.o.onFrame(new w.Int16Array(320));
  check(binaries(ws).length === 0, "nothing is sent while the button isn't held");
  $("ask").dispatchEvent(new w.MouseEvent("pointerdown", { bubbles: true }));
  check(strings(ws).includes('{"type":"ask_press"}') && binaries(ws).length === 5, "pressing sends ask_press and the 500 ms pre-roll");
  for (let i = 0; i < 3; i++) FakeMic.last.o.onFrame(new w.Int16Array(320));
  check(binaries(ws).length === 8, "live frames stream while held");
  await sleep(350);
  $("ask").dispatchEvent(new w.MouseEvent("pointerup", { bubbles: true }));
  check(!strings(ws).includes('{"type":"ask_release"}'), "ask_release waits for the post-roll");
  await sleep(400);
  check(strings(ws).includes('{"type":"ask_release"}'), "ask_release is sent after the post-roll");
  ws.receive(JSON.stringify({ type: "mode_changed", mode: "question" }));
  check(text($("state-pill")) === "Asking", "the pill shows Asking while the question is in flight");
  ws.receive(JSON.stringify({ type: "mode_changed", mode: "lecture" }));
  ws.receive(JSON.stringify({ type: "question_delivered", source_transcript: "What do you mean?", translated_text: "Apa maksud Anda?" }));
  const qa = w.document.querySelector(".qa-card");
  check(qa && text(qa).includes("What do you mean?") && text(qa).includes("Apa maksud Anda?"), "the delivered question is shown with its Indonesian translation");
  ws.receive(JSON.stringify({ type: "question_failed", error: "STT failed" }));
  check(w.document.querySelectorAll(".qa-card.error").length === 1, "a failed question is shown clearly");
  ws.serverClose(4413, "The lecture has ended");
  check(text($("status")).includes("The lecture has ended") && text($("state-pill")) === "Ended" && w.document.querySelectorAll(".caption").length === 1, "when the lecture ends the page says so and keeps the captions");
  check(FakeMic.last.stopped, "the microphone is released when the lecture ends");

  console.log("\nstudent page: refusals and retry");
  $("code").value = "ABCDEF"; $("join").click(); await sleep(30);
  p.last().serverClose(4412, "No lecture is live in this room yet");
  check(text($("status")).includes("No lecture is live") && text($("status")).includes("Checking again"), "no live lecture: the page waits and retries");
  const before = FakeWS.all.length;
  $("leave").click(); await sleep(1500);
  check(text($("status")).includes("You left") && FakeWS.all.length === before, "Leave cancels the retry");
  $("code").value = "ZZZZZZ"; $("join").click(); await sleep(30);
  p.last().serverClose(4404, "Unknown room code");
  check(text($("status")).includes("wasn't found"), "an unknown code is explained");
  $("join").click(); await sleep(30);
  p.last().serverClose(4403, "That room belongs to a different school");
  check(text($("status")).includes("different school"), "a room from another school is explained in the server's words");
  $("join").click(); await sleep(30);
  p.last().serverClose(4409, "A student is already connected");
  check(text($("status")).includes("Another student"), "a second student gets a clear message");
  $("join").click(); await sleep(30);
  p.last().serverClose(4401, "Sign in to join a lecture");
  check(p.redirects.includes("login.html") && p.stores.ss.getItem("blb.session") === null, "a rejected session clears it and sends the student to sign in");

  console.log("\nstudent page: single-session mode");
  const l = await load("student.html", { search: "?legacy=1" });
  l.$("join").click(); await sleep(30);
  check(l.redirects.length === 0 && l.last().url === "ws://localhost:8765/ws/student", "?legacy=1 needs no sign-in and connects without a code or token");
}

async function teacherPage() {
  console.log("\nlecturer page");
  let roomList = [
    { id: "r1", room_code: "CS-302", room_name: "Algorithms", join_code: "K7P2QX", mine: true, is_live: false },
    { id: "r2", room_code: "CS-303", room_name: null, join_code: "M4N8RT", mine: false, is_live: true },
  ];
  const routes = {
    "GET /rooms": () => ({ json: roomList }),
    "POST /rooms": (b) => {
      if (b.room_code === "DUP") return { status: 409, json: { detail: "A room with that label already exists" } };
      const r = { id: "r3", room_code: b.room_code, room_name: b.room_name, join_code: "ABCDEF", mine: true, is_live: false };
      roomList = roomList.concat([r]);
      return { status: 201, json: r };
    },
  };
  const p = await load("teacher.html", { session: session("LECTURER", "lec-tok", "Tara Teacher"), routes });
  const { w, $ } = p;
  check($("whoami").textContent === "Tara Teacher" && !$("signout").hidden, "the signed-in lecturer is shown");
  const opts = Array.from($("room-select").options).map((o) => o.textContent);
  check(opts.length === 2 && opts[0] === "CS-302 - Algorithms" && opts[1] === "CS-303 (live now)", "the lecturer's rooms are listed, with live ones marked");
  check($("room-select").value === "CS-302" && text($("join-code")) === "K7P2QX", "one of their own rooms is selected and its join code shown");
  $("room-select").value = "CS-303"; $("room-select").dispatchEvent(new w.Event("change"));
  check(text($("join-code")) === "M4N8RT", "changing the room changes the join code shown");
  $("new-room-label").value = ""; $("create-room").click(); await sleep(10);
  check(text($("status")).includes("Give the room a label"), "creating a room needs a label");
  $("new-room-label").value = "DUP"; $("create-room").click(); await sleep(30);
  check(text($("status")).includes("already exists"), "a duplicate label shows the server's message");
  $("new-room-label").value = "LAB-1"; $("new-room-name").value = "Lab one"; $("create-room").click(); await sleep(40);
  check(text($("status")).includes("ABCDEF") && $("room-select").value === "LAB-1" && text($("join-code")) === "ABCDEF", "a new room is created, selected, and its join code announced");

  $("room-select").value = "CS-302"; $("room-select").dispatchEvent(new w.Event("change"));
  $("start").click(); await sleep(60);
  const ws = p.last();
  check(ws.url === "ws://localhost:8765/ws/teacher?room=CS-302&token=lec-tok", "starting connects with the chosen room and the lecturer's token (" + ws.url.replace(/token=.*/, "token=***") + ")");
  check(FakeMic.last && $("start").disabled, "the microphone starts first and Start is disabled");
  FakeMic.last.o.onFrame(new w.Int16Array(320));
  check(ws.sent.length === 0, "no audio is sent before the server says the speech engine is ready");
  ws.receive(JSON.stringify({ type: "mode_changed", mode: "lecture" }));
  check(!$("live-card").hidden && $("setup-card").hidden && text($("live-join-code")) === "K7P2QX", "the first server message starts the live view, with the join code for the projector");
  FakeMic.last.o.onFrame(new w.Int16Array(320)); FakeMic.last.o.onFrame(new w.Int16Array(320));
  const seqs = () => ws.sent.map((b) => new w.DataView(b).getUint32(0, false));
  check(JSON.stringify(seqs()) === "[0,1]", "frames are sent with sequence numbers from 0");
  ws.receive(JSON.stringify({ type: "mode_changed", mode: "question" }));
  FakeMic.last.o.onFrame(new w.Int16Array(320));
  check(!$("mode-banner").hidden && ws.sent.length === 2, "while a student asks, a banner shows and the microphone is paused");
  ws.receive(JSON.stringify({ type: "mode_changed", mode: "lecture" }));
  FakeMic.last.o.onFrame(new w.Int16Array(320));
  check($("mode-banner").hidden && JSON.stringify(seqs()) === "[0,1,2]", "afterwards streaming resumes with contiguous sequence numbers");
  ws.receive(p.frame(0, [9, 9, 9]).slice(4)); await sleep(10);
  check(w.document.querySelectorAll("#questions li").length === 1 && FakeAudio.all.length === 1 && FakeAudio.all[0].played, "a student's question is logged and played");
  $("end").click(); await sleep(60);
  check(ws.closedWith === 1000 && FakeMic.last.stopped && text($("status")).includes("Lecture ended") && !$("setup-card").hidden, "End closes normally, releases the microphone and returns to setup");

  console.log("\nlecturer page: refusals");
  const refusals = [[4403, "Monthly audio limit reached (60 of 60 minutes used)", "Monthly audio limit reached"], [4409, "This room already has a live lecture", "already has a live lecture"], [1011, "DEEPGRAM_API_KEY not set", "Server error"]];
  for (const [code, reason, expect] of refusals) {
    $("start").click(); await sleep(60);
    p.last().serverClose(code, reason);
    check(text($("status")).includes(expect) && !$("start").disabled, code + ": " + expect);
  }
  $("start").click(); await sleep(60);
  p.last().receive(JSON.stringify({ type: "mode_changed", mode: "lecture" }));
  p.last().serverClose(1006, "");
  check(text($("status")).includes("Connection lost") && text($("status")).includes("start a new one"), "a dropped connection mid-lecture says it ended on the server");
  $("start").click(); await sleep(60);
  p.last().serverClose(4401, "Invalid or expired token");
  check(p.redirects.includes("login.html"), "a rejected session sends the lecturer to sign in");

  const s = await load("teacher.html", { session: session("STUDENT", "stu-tok") });
  check(s.redirects[0] === "login.html" && s.calls.length === 0, "a student's session doesn't open the lecturer page");
  const e = await load("teacher.html", { session: session("LECTURER", "lec-tok"), routes: { "GET /rooms": { json: [] } } });
  check(text(e.$("room-select")).includes("No rooms yet") && e.$("new-room-box").open, "with no rooms yet, the create-room form opens");
  const l = await load("teacher.html", { search: "?legacy=1" });
  l.$("start").click(); await sleep(60);
  check(l.redirects.length === 0 && /^ws:\/\/localhost:8765\/ws\/teacher\?session_id=web-/.test(l.last().url) && l.$("room-block").hidden, "?legacy=1 needs no sign-in or room and connects in single-session mode");
}

(async () => {
  await loginPage();
  await adminPage();
  await studentPage();
  await teacherPage();
  console.log(failures ? "\n" + failures + " check(s) FAILED" : "\nAll checks passed.");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
