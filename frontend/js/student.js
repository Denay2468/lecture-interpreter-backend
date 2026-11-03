/* Student page: join by room code, show live captions, play the translated audio, push-to-talk questions. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const el = {
    code: $("code"), join: $("join"), leave: $("leave"), serverUrl: $("server-url"), legacy: $("legacy"),
    status: $("status"), liveCard: $("live-card"), askCard: $("ask-card"), pill: $("state-pill"),
    modeText: $("mode-text"), sound: $("sound"), soundBanner: $("sound-banner"), enableSound: $("enable-sound"),
    captions: $("captions"), emptyNote: $("empty-note"), enableMic: $("enable-mic"), micNote: $("mic-note"),
    ask: $("ask"), askHint: $("ask-hint"), level: $("level"), whoami: $("whoami"), signout: $("signout"),
  };
  el.serverUrl.value = BLB.defaultServerUrl();
  const joinParams = new URLSearchParams(location.search);
  try {
    const incoming = joinParams.get("code");
    if (incoming) sessionStorage.setItem("blb.joinCode", incoming);
    el.code.value = incoming || sessionStorage.getItem("blb.joinCode") || "";
    const port = Number(joinParams.get("wport"));
    if (Number.isInteger(port) && port > 0 && port <= 65535) {
      const server = new URL(el.serverUrl.value); server.port = port;
      el.serverUrl.value = server.href.replace(/\/$/, ""); BLB.saveServerUrl(el.serverUrl.value);
    }
  } catch (_) {}
  let studentId = null, granted = false;
  const requestButton = $("request-question");
  requestButton.addEventListener("click", () => send(JSON.stringify({type: "request_question"})));
  const pcm = new BLB.PCMPlayer({
    onStart: (m) => send(JSON.stringify({type: "playback_started", sequence: m.sequence})),
    onBlocked: () => { el.soundBanner.hidden = false; },
    onBacklog: () => { setStatus("Audio fell behind. Reconnecting to live audio.", "busy"); if (ws) ws.close(1013); }
  });

  // Students sign in with their university email. (student.html?legacy=1 skips this for a server running without a database.)
  if (new URLSearchParams(location.search).get("legacy") === "1") {
    el.legacy.checked = true;
  } else {
    const session = BLB.api.requireRole("STUDENT");
    if (!session) return;
    el.whoami.textContent = session.user.full_name;
    el.signout.hidden = false;
    el.signout.addEventListener("click", async () => { await BLB.api.signOut(); BLB.api.goto("login.html"); });
  }

  let ws = null, retryTimer = null, userLeft = false, gotFirstMessage = false, mic = null;
  const captionEls = new Map();

  const queue = new BLB.AudioQueue({
    onStart: (m) => { send(JSON.stringify({type: "playback_started", sequence: m.sequence})); const c = captionEls.get(m.sequence); if (c) c.classList.add("playing"); },
    onEnd: (m) => { const c = captionEls.get(m.sequence); if (c) c.classList.remove("playing"); },
    onBlocked: () => { el.soundBanner.hidden = false; },
  });

  function send(data) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(data); }
  const ask = new BLB.AskController({
    sendText: (obj) => send(JSON.stringify(obj)),
    sendBinary: (buf) => send(buf),
    now: () => performance.now(),
  });

  // ---- small UI helpers ----
  function setStatus(text, kind) { el.status.textContent = text; el.status.className = "status" + (kind ? " " + kind : ""); }
  function setPill(text, kind) { el.pill.textContent = text; el.pill.className = "pill" + (kind ? " " + kind : ""); }
  function showLive(on) { el.liveCard.hidden = !on; el.askCard.hidden = !on; }
  function setLevel(v) { el.level.style.width = Math.min(100, Math.round(v * 300)) + "%"; }
  function appendCard(node) {
    const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 160;
    el.emptyNote.hidden = true;
    el.captions.appendChild(node);
    if (nearBottom && node.scrollIntoView) node.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  function paragraph(cls, text) { const p = document.createElement("p"); p.className = cls; p.textContent = text; return p; }

  // ---- connection ----
  async function openSocket() {
    const base = el.serverUrl.value.trim().replace(/\/+$/, "");
    const code = BLB.normalizeCode(el.code.value);
    let url = base + "/ws/student";
    if (!el.legacy.checked) {
      const token = await BLB.api.accessToken(); // fresh on every (re)connect: access tokens are short-lived
      if (!token) { BLB.api.goto("login.html"); return; }
      url += "?code=" + encodeURIComponent(code) + "&token=" + encodeURIComponent(token);
    }
    let sock;
    try { sock = new WebSocket(url); } catch (e) { finishSession("That server address isn't valid: " + base, "error"); return; }
    sock.binaryType = "arraybuffer";
    ws = sock;
    gotFirstMessage = false;
    sock.onmessage = (ev) => { if (sock === ws) handleMessage(ev.data); };
    sock.onclose = (ev) => { if (sock === ws) handleClosed(ev); };
    sock.onerror = () => { /* the reason arrives with the close event */ };
  }

  function clearRetry() { if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; } }
  function scheduleRetry(message) {
    let left = 4;
    const tick = () => {
      if (userLeft) return;
      if (left <= 0) { retryTimer = null; setStatus("Checking again...", "busy"); openSocket(); return; }
      setStatus(message + " Checking again in " + left + "s. You can leave this page open.", "busy");
      left -= 1;
      retryTimer = setTimeout(tick, 1000);
    };
    tick();
  }

  function finishSession(message, kind) {
    setStatus(message, kind);
    stopMic();
    el.join.disabled = false;
    el.leave.hidden = true;
  }

  function handleClosed(ev) {
    ask.reset();
    if (ev.code !== 4413) { pcm.clear(); queue.clear(); }
    granted = false;
    setAskUi(false);
    ws = null;
    const info = BLB.explainClose("student", ev.code, ev.reason);
    if (userLeft) { finishSession("You left the room.", ""); return; }
    if (ev.code === 4401 && !el.legacy.checked) { BLB.api.session.clear(); BLB.api.goto("login.html"); return; }
    if (info.kind === "retry" || (info.kind === "lost" && gotFirstMessage)) {
      setPill("Reconnecting", "warn");
      scheduleRetry(info.message);
      return;
    }
    if (info.kind === "ended") {
      setPill("Ended", "");
      el.modeText.textContent = "The lecture has ended.";
      finishSession(info.message, "ok");
      return;
    }
    setPill("Disconnected", "err");
    finishSession(info.message, "error");
  }

  // ---- messages from the server ----
  function handleMessage(data) {
    if (typeof data !== "string") {
      let frame;
      try { frame = BLB.decodePlaybackFrame(data); } catch (e) { return; }
      if (el.sound.checked) queue.enqueue(frame.audio, { sequence: frame.sequence });
      return;
    }
    let msg;
    try { msg = JSON.parse(data); } catch (e) { return; }
    switch (msg.type) {
      case "welcome": studentId = msg.student_id; break;
      case "question_granted":
        granted = true; el.ask.disabled = !mic;
        el.askHint.textContent = "Your turn. Hold to speak (up to 45 seconds)."; break;
      case "question_expired": granted = false; ask.reset(); setAskUi(false); el.ask.disabled = true; break;
      case "classroom":
        if (msg.speaker !== studentId) { granted = false; el.ask.disabled = true; }
        requestButton.disabled = msg.speaker === studentId || msg.waiting.includes(studentId);
        requestButton.textContent = msg.waiting.includes(studentId) ? "Waiting: " + (msg.waiting.indexOf(studentId)+1) : "Raise hand";
        break;
      case "audio_chunk":
        if (el.sound.checked) pcm.push(msg.pcm, msg.sequence); break;
      case "segment_end":
        if (msg.source_transcript && !captionEls.has(msg.sequence)) addCaption(msg);
        if (msg.audio_failed && captionEls.has(msg.sequence)) captionEls.get(msg.sequence).appendChild(paragraph("muted small", "Audio unavailable or interrupted; text is retained."));
        break;
      case "mode_changed": onMode(msg.mode); break;
      case "segment": addCaption(msg); break;
      case "question_delivered": addQuestionResult(msg); break;
      case "question_failed": addQuestionFailure(msg); break;
      default: break;
    }
  }

  function onMode(mode) {
    if (!gotFirstMessage) {
      gotFirstMessage = true;
      showLive(true);
      setStatus("Connected. Translations will appear as the lecturer speaks.", "ok");
    }
    if (mode === "question") pcm.pause();
    else if (pcm.context) pcm.resume().catch(() => { el.soundBanner.hidden = false; });
    if (mode === "question") { setPill("Asking", "warn"); el.modeText.textContent = "A student is asking a question..."; }
    else { setPill("Live", "ok"); el.modeText.textContent = "Listening to the lecture"; }
  }

  function addCaption(msg) {
    const card = document.createElement("article");
    card.className = "caption";
    card.appendChild(paragraph("caption-text", msg.translated_text || ""));
    if (msg.source_transcript) card.appendChild(paragraph("caption-source", msg.source_transcript));
    captionEls.set(msg.sequence, card);
    appendCard(card);
  }

  function addQuestionResult(msg) {
    const card = document.createElement("article");
    card.className = "qa-card";
    card.appendChild(paragraph("muted small", "Your question"));
    card.appendChild(paragraph("caption-text", msg.source_transcript || ""));
    card.appendChild(paragraph("caption-source", "Translated for the lecturer: " + (msg.translated_text || "")));
    appendCard(card);
  }

  function addQuestionFailure(msg) {
    const card = document.createElement("article");
    card.className = "qa-card error";
    card.appendChild(paragraph("caption-text", "Your question didn't go through. Hold the button and try again."));
    if (msg.error) card.appendChild(paragraph("caption-source", String(msg.error)));
    appendCard(card);
  }

  // ---- microphone and ask button ----
  function stopMic() {
    if (mic) { mic.stop(); mic = null; }
    el.enableMic.hidden = false;
    el.enableMic.disabled = false;
    el.ask.disabled = true;
    el.askHint.textContent = "";
    setLevel(0);
  }

  el.enableMic.addEventListener("click", async () => {
    el.enableMic.disabled = true;
    try {
      const m = new BLB.MicStreamer({ onFrame: (s) => ask.onFrame(s), onLevel: setLevel });
      await m.start();
      mic = m;
      el.enableMic.hidden = true;
      el.ask.disabled = !granted;
      el.askHint.textContent = granted ? "Hold the button while you speak." : "Raise your hand and wait for the lecturer.";
    } catch (e) {
      el.enableMic.disabled = false;
      el.micNote.textContent = "Microphone unavailable: " + (e && e.message ? e.message : e);
    }
  });

  function setAskUi(on) { el.ask.classList.toggle("active", on); el.ask.textContent = on ? "Release to send" : "Hold to ask"; }
  function pressAsk(e) {
    if (el.ask.disabled || !granted) return;
    if (e) {
      if (e.preventDefault) e.preventDefault();
      if (e.pointerId !== undefined && el.ask.setPointerCapture) { try { el.ask.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ } }
    }
    ask.press();
    setAskUi(true);
  }
  function releaseAsk() { ask.release(); setAskUi(false); }
  el.ask.addEventListener("pointerdown", pressAsk);
  ["pointerup", "pointercancel", "lostpointercapture"].forEach((t) => el.ask.addEventListener(t, releaseAsk));
  el.ask.addEventListener("keydown", (e) => { if ((e.key === " " || e.key === "Enter") && !e.repeat) { e.preventDefault(); pressAsk(); } });
  el.ask.addEventListener("keyup", (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); releaseAsk(); } });
  el.ask.addEventListener("contextmenu", (e) => e.preventDefault());
  window.addEventListener("blur", releaseAsk);

  // ---- buttons ----
  el.join.addEventListener("click", () => {
    const code = BLB.normalizeCode(el.code.value);
    if (!el.legacy.checked && code.length < 6) {
      setStatus("Enter the room code shown by your lecturer (6 characters).", "error");
      el.code.focus();
      return;
    }
    pcm.resume().catch(() => { el.soundBanner.hidden = false; });
    BLB.saveServerUrl(el.serverUrl.value.trim());
    el.code.value = code;
    userLeft = false;
    clearRetry();
    el.join.disabled = true;
    el.leave.hidden = false;
    setStatus("Connecting...", "busy");
    openSocket();
  });
  el.code.addEventListener("keydown", (e) => { if (e.key === "Enter") el.join.click(); });

  el.leave.addEventListener("click", () => {
    userLeft = true;
    clearRetry();
    queue.clear();
    pcm.clear();
    if (ws) ws.close(1000, "left"); else finishSession("You left the room.", "");
  });

  el.sound.addEventListener("change", () => { if (!el.sound.checked) { queue.clear(); pcm.clear(); } });
  el.enableSound.addEventListener("click", () => { el.soundBanner.hidden = true; queue.resume(); pcm.resume().catch(() => { el.soundBanner.hidden = false; }); });
  window.addEventListener("beforeunload", () => { if (ws) ws.close(1000); });
})();
