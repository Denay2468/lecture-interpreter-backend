/* PCM16LE/24kHz streaming playback. No decodeAudioData on incomplete MP3 files. */
(function (root) {
  "use strict";
  class PCMPlayer {
    constructor(options) {
      this.o = options || {}; this.context = null; this.next = 0;
      this.nodes = new Set(); this.timers = new Set(); this.lastSequence = null;
    }
    async resume() {
      if (!this.context) this.context = new (root.AudioContext || root.webkitAudioContext)();
      await this.context.resume();
    }
    pause() { if (this.context) this.context.suspend(); }
    push(base64, sequence) {
      if (!this.context) {
        this.context = new (root.AudioContext || root.webkitAudioContext)();
      }
      const ctx = this.context;
      if (ctx.state !== "running" && this.o.onBlocked) this.o.onBlocked();
      if (this.next - ctx.currentTime > 8) {
        this.clear();
        if (this.o.onBacklog) this.o.onBacklog();
        return;
      }
      const raw = atob(base64);
      if (!raw.length || raw.length % 2) return;
      const buffer = ctx.createBuffer(1, raw.length / 2, 24000);
      const samples = buffer.getChannelData(0);
      for (let i = 0; i < samples.length; i++) {
        let v = raw.charCodeAt(i * 2) | raw.charCodeAt(i * 2 + 1) << 8;
        if (v >= 32768) v -= 65536;
        samples[i] = v / 32768;
      }
      const node = ctx.createBufferSource();
      node.buffer = buffer; node.connect(ctx.destination);
      const when = Math.max(ctx.currentTime + .04, this.next);
      this.next = when + buffer.duration;
      this.nodes.add(node);
      node.onended = () => { this.nodes.delete(node); node.disconnect(); };
      node.start(when);
      if (this.lastSequence !== sequence) {
        this.lastSequence = sequence;
        const notify = () => {
          if (ctx.state === "running" && ctx.currentTime >= when) {
            if (this.o.onStart) this.o.onStart({sequence});
          } else {
            const timer = setTimeout(() => { this.timers.delete(timer); notify(); }, 20);
            this.timers.add(timer);
          }
        };
        notify();
      }
    }
    clear() {
      for (const timer of this.timers) clearTimeout(timer);
      this.timers.clear();
      for (const node of this.nodes) { try { node.stop(); node.disconnect(); } catch (_) {} }
      this.nodes.clear(); this.next = 0; this.lastSequence = null;
    }
  }
  root.BLB.PCMPlayer = PCMPlayer;
  if (typeof module !== "undefined" && module.exports) module.exports = PCMPlayer;
})(typeof globalThis !== "undefined" ? globalThis : window);
