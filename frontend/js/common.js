/* BLB web clients: shared helpers. Plain script, no build step.
   Exposes window.BLB (and module.exports so the Node tests can load it). */
(function (root) {
  "use strict";

  const SAMPLE_RATE = 16000;
  const FRAME_SAMPLES = 320; // 20 ms at 16 kHz: the frame size the backend expects (app/protocol.py)
  const noop = () => {};

  // ---- wire format ---------------------------------------------------------
  // client -> server audio : [uint32 BE sequence][uint32 BE capture ms][PCM16 little-endian, mono, 16 kHz]
  // server -> student audio: [uint32 BE sequence][MP3 bytes]
  function encodeFrame(sequence, captureMs, samples) {
    const buffer = new ArrayBuffer(8 + samples.length * 2);
    const view = new DataView(buffer);
    view.setUint32(0, sequence >>> 0, false);
    view.setUint32(4, captureMs >>> 0, false);
    for (let i = 0; i < samples.length; i++) view.setInt16(8 + i * 2, samples[i], true);
    return buffer;
  }

  function decodePlaybackFrame(arrayBuffer) {
    if (arrayBuffer.byteLength < 4) throw new Error("playback frame too short");
    const sequence = new DataView(arrayBuffer).getUint32(0, false);
    return { sequence, audio: new Blob([arrayBuffer.slice(4)], { type: "audio/mpeg" }) };
  }

  function rms(samples) {
    let sum = 0;
    for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
    return Math.sqrt(sum / (samples.length || 1)) / 32768;
  }

  // ---- small helpers -------------------------------------------------------
  function normalizeCode(raw) {
    return String(raw || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
  }

  function storageGet(key) {
    try { return root.localStorage.getItem(key); } catch (e) { return null; }
  }
  function storageSet(key, value) {
    try { root.localStorage.setItem(key, value); } catch (e) { /* private mode, or file:// */ }
  }
  function defaultServerUrl() {
    const saved = storageGet("blb.serverUrl");
    if (saved) return saved;
    const loc = root.location || {};
    const host = loc.hostname || "localhost";
    return (loc.protocol === "https:" ? "wss" : "ws") + "://" + host + ":8765";
  }
  function saveServerUrl(url) { if (url) storageSet("blb.serverUrl", url); }

  // What a WebSocket close code from the backend means, for people.
  // kind: "retry" (try again shortly) | "ended" | "fatal" (fix something and retry) | "lost" (network)
  function explainClose(role, code, reason) {
    const detail = reason ? " (" + reason + ")" : "";
    const student = role === "student";
    switch (code) {
      case 4400: return { kind: "fatal", message: "The server rejected the request" + detail + "." };
      case 4401: return { kind: "fatal", message: "Your session has ended. Please sign in again." };
      case 4403: return { kind: "fatal", message: reason || (student ? "You can't join this lecture." : "This lecture can't be started (subscription or monthly limit).") };
      case 4404: return { kind: "fatal", message: student ? "That room code wasn't found. Check it and try again." : "Room not found. Check the room label." };
      case 4409: return { kind: "fatal", message: student ? "Another student is already connected to this room (one per room for now)." : (reason || "This room already has a live lecture.") };
      case 4412: return { kind: "retry", message: "No lecture is live in this room yet." };
      case 4413: return { kind: "ended", message: "The lecture has ended." };
      case 1000: case 1001: return { kind: "ended", message: "Disconnected." };
      case 1011: return { kind: "fatal", message: "Server error" + detail + "." };
      case 1006: return { kind: "lost", message: "Couldn't reach the server. Is it running at the address under Advanced?" };
      default: return { kind: "lost", message: "Connection closed (" + code + ")" + detail + "." };
    }
  }

  // ---- push-to-talk --------------------------------------------------------
  // Keeps a rolling pre-roll of recent microphone frames while idle. Pressing sends ask_press, flushes the
  // pre-roll, then streams live; releasing keeps streaming for a short post-roll (so the last word isn't
  // clipped) before ask_release. A very short tap skips the post-roll so the server's debounce discards it.
  class AskController {
    constructor(o) {
      this._sendText = o.sendText;
      this._sendBinary = o.sendBinary;
      this._now = o.now || (() => Date.now());
      this._setTimer = o.setTimer || ((fn, ms) => setTimeout(fn, ms));
      this._clearTimer = o.clearTimer || ((id) => clearTimeout(id));
      this._preRoll = o.preRollFrames == null ? 25 : o.preRollFrames;   // 500 ms
      this._postRollMs = o.postRollMs == null ? 300 : o.postRollMs;
      this._minHoldMs = o.minHoldMs == null ? 300 : o.minHoldMs;        // the server's QUESTION_MIN_DURATION_MS
      this._ring = [];
      this._asking = false;
      this._seq = 0;
      this._pressAt = 0;
      this._timer = null;
    }
    get asking() { return this._asking; }
    onFrame(samples) {
      if (this._asking) { this._sendFrame(samples); return; }
      this._ring.push(samples);
      if (this._ring.length > this._preRoll) this._ring.shift();
    }
    press() {
      if (this._timer !== null) { this._clearTimer(this._timer); this._timer = null; return; } // pressed again during post-roll: same question
      if (this._asking) return;
      this._asking = true;
      this._pressAt = this._now();
      this._seq = 0;
      this._sendText({ type: "ask_press" });
      const preRoll = this._ring;
      this._ring = [];
      for (const s of preRoll) this._sendFrame(s);
    }
    release() {
      if (!this._asking || this._timer !== null) return;
      if (this._now() - this._pressAt < this._minHoldMs) { this._finish(); return; }
      this._timer = this._setTimer(() => { this._timer = null; this._finish(); }, this._postRollMs);
    }
    reset() {
      if (this._timer !== null) { this._clearTimer(this._timer); this._timer = null; }
      this._asking = false;
      this._ring = [];
    }
    _finish() { this._asking = false; this._sendText({ type: "ask_release" }); }
    _sendFrame(samples) { this._sendBinary(encodeFrame(this._seq, this._seq * 20, samples)); this._seq += 1; }
  }

  // ---- microphone ----------------------------------------------------------
  // Runs in an AudioWorklet: area-averaging resampler from the device rate (48k, 44.1k...) down to
  // 16 kHz mono, emitted as 320-sample Int16 frames. Loaded from a Blob so it also works from file://.
  const WORKLET_SOURCE = `
    class PCM16kProcessor extends AudioWorkletProcessor {
      constructor() {
        super();
        this.ratio = sampleRate / ${SAMPLE_RATE};
        this.need = this.ratio;
        this.sum = 0;
        this.out = new Int16Array(${FRAME_SAMPLES});
        this.n = 0;
      }
      process(inputs) {
        const channel = inputs[0] && inputs[0][0];
        if (!channel) return true;
        for (let i = 0; i < channel.length; i++) {
          const x = channel[i];
          let remaining = 1;
          while (remaining > 1e-9) {
            const take = remaining < this.need ? remaining : this.need;
            this.sum += x * take;
            this.need -= take;
            remaining -= take;
            if (this.need <= 1e-9) {
              let v = this.sum / this.ratio;
              this.sum = 0;
              this.need = this.ratio;
              v = v < -1 ? -1 : (v > 1 ? 1 : v);
              this.out[this.n++] = v < 0 ? v * 32768 : v * 32767;
              if (this.n === this.out.length) {
                const frame = this.out.slice();
                this.n = 0;
                this.port.postMessage(frame.buffer, [frame.buffer]);
              }
            }
          }
        }
        return true;
      }
    }
    registerProcessor("pcm16k", PCM16kProcessor);
  `;

  class MicStreamer {
    constructor(o) {
      this.onFrame = o.onFrame;
      this.onLevel = o.onLevel || noop;
      this._stream = null;
      this._ctx = null;
      this._node = null;
    }
    async start() {
      const md = root.navigator && root.navigator.mediaDevices;
      if (!md || !md.getUserMedia) throw new Error("this browser can't reach the microphone (open the page on localhost or over https)");
      this._stream = await md.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      const Ctx = root.AudioContext || root.webkitAudioContext;
      this._ctx = new Ctx();
      const url = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: "application/javascript" }));
      try { await this._ctx.audioWorklet.addModule(url); } finally { URL.revokeObjectURL(url); }
      const source = this._ctx.createMediaStreamSource(this._stream);
      this._node = new root.AudioWorkletNode(this._ctx, "pcm16k");
      this._node.port.onmessage = (e) => {
        const samples = new Int16Array(e.data);
        this.onFrame(samples);
        this.onLevel(rms(samples));
      };
      const mute = this._ctx.createGain(); // the node must be connected to the graph to run; keep it silent
      mute.gain.value = 0;
      source.connect(this._node);
      this._node.connect(mute);
      mute.connect(this._ctx.destination);
      if (this._ctx.state === "suspended") await this._ctx.resume();
    }
    stop() {
      try { if (this._node) { this._node.port.onmessage = null; this._node.disconnect(); } } catch (e) { /* already gone */ }
      if (this._stream) this._stream.getTracks().forEach((t) => t.stop());
      if (this._ctx) this._ctx.close().catch(noop);
      this._stream = this._ctx = this._node = null;
    }
  }

  // ---- playback ------------------------------------------------------------
  function defaultPlayer(blob) {
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    const p = {
      onended: null, onerror: null,
      play: () => audio.play(),
      stop: () => audio.pause(),
      dispose: () => { audio.src = ""; URL.revokeObjectURL(url); },
    };
    audio.addEventListener("ended", () => p.onended && p.onended());
    audio.addEventListener("error", () => p.onerror && p.onerror());
    return p;
  }

  // Plays clips one after another, in the order they were queued.
  class AudioQueue {
    constructor(o) {
      o = o || {};
      this.onStart = o.onStart || noop;
      this.onEnd = o.onEnd || noop;
      this.onBlocked = o.onBlocked || noop;
      this._create = o.createPlayer || defaultPlayer;
      this._queue = [];
      this._current = null;
      this._blocked = false;
    }
    enqueue(blob, meta) { this._queue.push({ blob, meta }); this._pump(); }
    resume() { this._blocked = false; this._pump(); }
    clear() {
      this._queue.length = 0;
      const c = this._current;
      if (!c) return;
      this._current = null;
      c.player.onended = c.player.onerror = null;
      try { if (c.player.stop) c.player.stop(); } catch (e) { /* ignore */ }
      c.player.dispose();
      this.onEnd(c.meta);
    }
    _pump() {
      if (this._current || this._blocked || this._queue.length === 0) return;
      const item = this._queue.shift();
      const player = this._create(item.blob);
      this._current = { player, meta: item.meta };
      const finish = () => {
        if (!this._current || this._current.player !== player) return;
        this._current = null;
        player.dispose();
        this.onEnd(item.meta);
        this._pump();
      };
      player.onended = finish;
      player.onerror = finish;
      player.play().then(
        () => this.onStart(item.meta),
        () => { // the browser wants a user gesture first: keep the clip and wait for resume()
          if (!this._current || this._current.player !== player) return;
          this._queue.unshift(item);
          this._current = null;
          player.dispose();
          this._blocked = true;
          this.onBlocked();
        });
    }
  }

  const BLB = {
    SAMPLE_RATE, FRAME_SAMPLES, WORKLET_SOURCE,
    encodeFrame, decodePlaybackFrame, rms, normalizeCode, explainClose,
    defaultServerUrl, saveServerUrl, AskController, MicStreamer, AudioQueue,
  };
  root.BLB = BLB;
  if (typeof module !== "undefined" && module.exports) module.exports = BLB;
})(typeof globalThis !== "undefined" ? globalThis : window);
