/* Lecturer page: pick or create a room, start a lecture, stream the microphone, hear student questions. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const el = {
    roomBlock: $("room-block"), roomSelect: $("room-select"), joinLine: $("join-line"), joinCode: $("join-code"),
    newRoomBox: $("new-room-box"), newLabel: $("new-room-label"), newName: $("new-room-name"), createRoom: $("create-room"),
    serverUrl: $("server-url"), legacy: $("legacy"), start: $("start"), status: $("status"),
    setupCard: $("setup-card"), liveCard: $("live-card"), timer: $("timer"), modeBanner: $("mode-banner"),
    level: $("level"), end: $("end"), questions: $("questions"), questionsEmpty: $("questions-empty"), note: $("note"),
    liveJoinLine: $("live-join-line"), liveJoinCode: $("live-join-code"), whoami: $("whoami"), signout: $("signout"),
    historyCard: $("history-card"), history: $("history"), historyEmpty: $("history-empty"), attendancePanel: $("attendance-panel"),
  };
  el.serverUrl.value = BLB.defaultServerUrl();

  let ws = null, mic = null, state = "idle", mode = "lecture", seq = 0, liveAt = 0, timerId = null, userEnded = false;
  let rooms = [];

  const queue = new BLB.AudioQueue({
    onBlocked: () => {
      el.note.textContent = "Click anywhere on the page to hear the student's question.";
      document.addEventListener("click", () => { el.note.textContent = ""; queue.resume(); }, { once: true });
    },
  });

  function setStatus(text, kind) { el.status.textContent = text; el.status.className = "status" + (kind ? " " + kind : ""); }
  function setLevel(v) { el.level.style.width = Math.min(100, Math.round(v * 300)) + "%"; }
  function fmt(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    const two = (n) => String(n).padStart(2, "0");
    return (h ? two(h) + ":" : "") + two(m) + ":" + two(sec);
  }

  // ---- rooms ----
  function selectedRoom() { return rooms.find((r) => r.room_code === el.roomSelect.value); }
  function showJoinCode() {
    const r = selectedRoom();
    el.joinLine.hidden = !r;
    if (r) el.joinCode.textContent = r.join_code;
  }
  async function loadRooms(selectLabel) {
    try { rooms = await BLB.api.get("/rooms"); }
    catch (e) {
      if (e.status === 401) { BLB.api.goto("login.html"); return; }
      setStatus(e.message, "error");
      return;
    }
    el.roomSelect.replaceChildren();
    if (!rooms.length) {
      const o = document.createElement("option");
      o.value = ""; o.textContent = "No rooms yet: create one below";
      el.roomSelect.appendChild(o);
      el.newRoomBox.open = true;
    }
    rooms.forEach((r) => {
      const o = document.createElement("option");
      o.value = r.room_code;
      o.textContent = r.room_code + (r.room_name ? " - " + r.room_name : "") + (r.is_live ? " (live now)" : "");
      el.roomSelect.appendChild(o);
    });
    const pick = selectLabel || (rooms.find((r) => r.mine) || rooms[0] || {}).room_code;
    if (pick) el.roomSelect.value = pick;
    showJoinCode();
  }
  el.roomSelect.addEventListener("change", showJoinCode);
  el.createRoom.addEventListener("click", async () => {
    const label = el.newLabel.value.trim();
    if (!label) { setStatus("Give the room a label, such as CS-302.", "error"); return; }
    try {
      const r = await BLB.api.post("/rooms", { room_code: label, room_name: el.newName.value.trim() || null });
      el.newLabel.value = ""; el.newName.value = "";
      setStatus("Room " + r.room_code + " created. Students join with the code " + r.join_code + ".", "ok");
      await loadRooms(r.room_code);
    } catch (e) { setStatus(e.message, "error"); }
  });

  // ---- recent lectures and who attended ----
  async function loadHistory() {
    try {
      const rows = await BLB.api.get("/sessions?limit=10");
      BLB.attendance.renderSessions(el.history.querySelector("tbody"), el.attendancePanel, rows, { showLecturer: false });
      el.historyEmpty.hidden = rows.length > 0;
    } catch (e) { if (e.status === 401) BLB.api.goto("login.html"); }
  }

  // ---- sign-in gate (teacher.html?legacy=1 skips it, for a server running without a database) ----
  if (new URLSearchParams(location.search).get("legacy") === "1") {
    el.legacy.checked = true;
    el.roomBlock.hidden = true;
    el.historyCard.hidden = true;
  } else {
    const session = BLB.api.requireRole("ADMIN", "LECTURER");
    if (!session) return;
    el.whoami.textContent = session.user.full_name;
    el.signout.hidden = false;
    el.signout.addEventListener("click", async () => { await BLB.api.signOut(); BLB.api.goto("login.html"); });
    loadRooms();
    loadHistory();
  }
  BLB.courseProfiles.init();
  el.legacy.addEventListener("change", () => { el.roomBlock.hidden = el.legacy.checked; });

  // ---- the lecture ----
  function backToSetup(message, kind) {
    state = "idle";
    clearInterval(timerId); timerId = null;
    if (mic) { mic.stop(); mic = null; }
    queue.clear();
    setLevel(0);
    el.liveCard.hidden = true;
    el.setupCard.hidden = false;
    el.start.disabled = false;
    el.end.disabled = false;
    setStatus(message, kind);
    if (!el.legacy.checked) { loadRooms(el.roomSelect.value); loadHistory(); } // refresh the "live now" markers and the new lecture
  }

  function onFrame(samples) {
    // Frames are only sent once the lecture is live, and not while a student is asking (Rule 4: the
    // server wouldn't use them anyway). Sequence numbers stay contiguous across the pause.
    if (state !== "live" || mode !== "lecture" || !ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(BLB.encodeFrame(seq, Math.round(performance.now() - liveAt), samples));
    seq += 1;
  }

  function goLive() {
    state = "live";
    mode = "lecture";
    seq = 0;
    liveAt = performance.now();
    el.setupCard.hidden = true;
    el.liveCard.hidden = false;
    el.modeBanner.hidden = true;
    const r = el.legacy.checked ? null : selectedRoom();
    el.liveJoinLine.hidden = !r;
    if (r) {
      el.liveJoinCode.textContent = r.join_code;
      const join = new URL("student.html", location.href); join.searchParams.set("code", r.join_code);
      join.searchParams.set("wport", new URL(el.serverUrl.value).port || "8765");
      $("join-link").href = join.href;
      const qr = qrcode(0, "M"); qr.addData(join.href); qr.make();
      $("join-qr").innerHTML = qr.createSvgTag({cellSize:3, margin:12, scalable:false});
    }
    $("join-link").hidden = !r;
    $("join-qr").hidden = !r;
    el.timer.textContent = "00:00";
    timerId = setInterval(() => { el.timer.textContent = fmt(performance.now() - liveAt); }, 500);
  }

  function addQuestionEntry() {
    el.questionsEmpty.hidden = true;
    const li = document.createElement("li");
    li.textContent = new Date().toLocaleTimeString() + "  Question from a student (playing in your selected language)";
    el.questions.prepend(li);
  }

  function handleMessage(data) {
    if (typeof data !== "string") {
      queue.enqueue(new Blob([data], { type: "audio/mpeg" }), {});
      addQuestionEntry();
      return;
    }
    let msg;
    try { msg = JSON.parse(data); } catch (e) { return; }
    if (msg.type === "classroom") {
      $("classroom-count").textContent = msg.students + " students connected";
      $("question-queue").replaceChildren();
      msg.waiting.forEach((id, index) => {
        const button = document.createElement("button");
        const who = msg.names && msg.names[id];
        button.textContent = "Allow " + (who || "student " + (index+1) + " (" + id.slice(-4) + ")");
        button.disabled = !!msg.speaker;
        button.onclick = () => ws.send(JSON.stringify({type:"grant_question", student_id:id}));
        $("question-queue").appendChild(button);
      });
      return;
    }
    if (msg.type === "metrics") {
      const ms = (v) => v == null ? "—" : Math.round(v) + " ms";
      $("session-metrics").textContent = "Audio delivery: " + msg.delivered + " successful / " + msg.failed + " failed. Success: " + (msg.success_percent == null ? "—" : msg.success_percent + "%") +
        ". Phrase-ready → playback report p50: " + ms(msg.segment_to_playback_ack_p50_ms) + ", p95: " + ms(msg.segment_to_playback_ack_p95_ms) + " (" + msg.playback_samples + " reports). MT characters: " + msg.mt_characters + "; TTS characters: " + msg.tts_characters +
        ". Estimated provider cost: " + (msg.estimated_cost == null ? "configure rates in .env" : msg.estimated_cost.toFixed(4) + " " + msg.cost_currency + "; hourly projection " + msg.projected_hourly_cost.toFixed(3));
      return;
    }
    if (msg.type !== "mode_changed") return;
    if (state === "starting") goLive(); // the first message means the speech engine is ready
    mode = msg.mode === "question" ? "question" : "lecture";
    el.modeBanner.hidden = mode !== "question";
  }

  function handleClosed(ev) {
    ws = null;
    const info = BLB.explainClose("teacher", ev.code, ev.reason);
    const wasLive = state === "live";
    const elapsed = wasLive ? performance.now() - liveAt : 0;
    if (userEnded) { backToSetup("Lecture ended. It ran for " + fmt(elapsed) + ".", "ok"); return; }
    if (ev.code === 4401 && !el.legacy.checked) { BLB.api.session.clear(); BLB.api.goto("login.html"); return; }
    if (wasLive && (info.kind === "lost" || info.kind === "ended")) {
      backToSetup("Connection lost after " + fmt(elapsed) + ". The server ends a lecture when the lecturer's connection drops, so start a new one to continue.", "error");
      return;
    }
    backToSetup(info.message, info.kind === "ended" ? "ok" : "error");
  }

  el.start.addEventListener("click", async () => {
    if (state !== "idle") return;
    let lectureSettings;
    try { lectureSettings = {...BLB.courseProfiles.settings(), ...(BLB.courseMaterials ? BLB.courseMaterials.settings() : {})}; }
    catch (e) { setStatus(e.message, "error"); return; }
    const sourceLanguage = $("source-language").value;
    const legacy = el.legacy.checked;
    let room = "", token = "";
    if (!legacy) {
      const r = selectedRoom();
      if (!r) { setStatus("Choose a room, or create one first.", "error"); return; }
      room = r.room_code;
      token = await BLB.api.accessToken();
      if (!token) { BLB.api.goto("login.html"); return; }
    }
    BLB.saveServerUrl(el.serverUrl.value.trim());

    state = "starting";
    userEnded = false;
    el.start.disabled = true;
    setStatus("Requesting microphone access...", "busy");
    try {
      const m = new BLB.MicStreamer({ onFrame, onLevel: setLevel });
      await m.start();
      mic = m;
    } catch (e) {
      backToSetup("Microphone unavailable: " + (e && e.message ? e.message : e), "error");
      return;
    }
    setStatus("Starting the lecture... the speech engine needs a few seconds to warm up.", "busy");

    const base = el.serverUrl.value.trim().replace(/\/+$/, "");
    let url = legacy
      ? base + "/ws/teacher?session_id=web-" + Math.random().toString(36).slice(2, 8)
      : base + "/ws/teacher?room=" + encodeURIComponent(room) + "&token=" + encodeURIComponent(token);
    url += "&source=" + encodeURIComponent(sourceLanguage) + "&configure=1";
    let sock;
    try { sock = new WebSocket(url); } catch (e) { backToSetup("That server address isn't valid: " + base, "error"); return; }
    sock.binaryType = "arraybuffer";
    sock.onopen = () => sock.send(JSON.stringify({type:"configure_lecture", ...lectureSettings}));
    ws = sock;
    sock.onmessage = (ev) => { if (sock === ws) handleMessage(ev.data); };
    sock.onclose = (ev) => { if (sock === ws) handleClosed(ev); };
    sock.onerror = () => { /* the reason arrives with the close event */ };
  });

  el.end.addEventListener("click", () => {
    userEnded = true;
    el.end.disabled = true;
    if (ws) ws.close(1000, "lecturer ended"); else backToSetup("Lecture ended.", "ok");
  });
  window.addEventListener("beforeunload", () => { if (ws) ws.close(1000); });
})();
