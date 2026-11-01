/* Lecture lists with attendee counts, and the attendance sheet for one lecture (on screen and as a CSV).
   Used by the admin dashboard and the lecturer page. Needs common.js and api.js first. */
(function (root) {
  "use strict";
  const BLB = root.BLB;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function cell(tr, text) { const td = document.createElement("td"); td.textContent = text; tr.appendChild(td); return td; }
  function fmtLength(s) {
    if (s == null) return "running";
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? h + " h " + m + " min" : m + " min";
  }
  function fmtWhen(iso) { return iso ? new Date(iso).toLocaleString() : ""; }
  function statusBadge(status) {
    const kind = status === "LIVE" ? "ok" : status === "ABORTED" ? "warn" : "";
    return el("span", "badge" + (kind ? " " + kind : ""), status === "LIVE" ? "Live" : status === "COMPLETED" ? "Completed" : "Interrupted");
  }

  // One row per lecture, with a button that opens its attendance sheet in `panel`.
  function renderSessions(tbody, panel, sessions, opts) {
    opts = opts || {};
    tbody.replaceChildren();
    sessions.forEach((s) => {
      const tr = document.createElement("tr");
      cell(tr, s.room_code);
      if (opts.showLecturer) cell(tr, s.lecturer_name);
      cell(tr, fmtWhen(s.started_at));
      cell(tr, fmtLength(s.duration_seconds));
      cell(tr, String(s.attendee_count));
      cell(tr, "").appendChild(statusBadge(s.status));
      const b = el("button", "secondary small", "Attendance");
      b.type = "button";
      b.addEventListener("click", () => showAttendance(panel, s));
      cell(tr, "").appendChild(b);
      tbody.appendChild(tr);
    });
  }

  function saveFile(name, text) {
    const url = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
    const a = document.createElement("a");
    a.href = url; a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  async function showAttendance(panel, s) {
    panel.hidden = false;
    panel.replaceChildren(el("p", "muted", "Loading attendance..."));
    let data;
    try { data = await BLB.api.get("/sessions/" + s.id + "/attendance"); }
    catch (e) {
      if (e.status === 401) { BLB.api.goto("login.html"); return; }
      panel.replaceChildren(el("p", "status error", e.message));
      return;
    }
    panel.replaceChildren();
    panel.appendChild(el("h3", "", "Attendance: " + data.session.room_code + ", " + fmtWhen(data.session.started_at)));
    const n = data.attendees.length;
    panel.appendChild(el("p", "muted small", n
      ? n + (n === 1 ? " student joined" : " students joined") + (data.session.status === "LIVE" ? " so far (the lecture is still live)." : ".")
      : "No students joined this lecture."));
    if (n) {
      const wrap = el("div", "table-wrap"), table = document.createElement("table"), head = document.createElement("tr");
      ["Name", "Email", "First joined", "Time connected", "Of the lecture", "Times connected"].forEach((h) => head.appendChild(el("th", "", h)));
      const thead = document.createElement("thead"), tbody = document.createElement("tbody");
      thead.appendChild(head);
      data.attendees.forEach((a) => {
        const tr = document.createElement("tr");
        cell(tr, a.full_name + (a.connected_now ? " (connected now)" : ""));
        cell(tr, a.email);
        cell(tr, fmtWhen(a.first_joined_at));
        cell(tr, Math.round(a.connected_seconds / 60) + " min");
        cell(tr, a.percent + "%");
        cell(tr, String(a.connections));
        tbody.appendChild(tr);
      });
      table.appendChild(thead); table.appendChild(tbody); wrap.appendChild(table);
      panel.appendChild(wrap);
    }
    const row = el("div", "row"), status = el("p", "status");
    const dl = el("button", "secondary", "Download CSV");
    dl.type = "button";
    dl.addEventListener("click", async () => {
      status.textContent = ""; status.className = "status";
      try {
        const text = await BLB.api.download("/sessions/" + s.id + "/attendance.csv");
        saveFile("attendance-" + data.session.room_code.replace(/[^A-Za-z0-9._-]/g, "_") + "-" + String(data.session.started_at).slice(0, 10) + ".csv", text);
      } catch (e) { status.textContent = e.message; status.className = "status error"; }
    });
    const close = el("button", "ghost", "Close");
    close.type = "button";
    close.addEventListener("click", () => { panel.hidden = true; panel.replaceChildren(); });
    row.appendChild(dl); row.appendChild(close);
    panel.appendChild(row); panel.appendChild(status);
  }

  BLB.attendance = { renderSessions, showAttendance };
  if (typeof module !== "undefined" && module.exports) module.exports = BLB.attendance;
})(typeof globalThis !== "undefined" ? globalThis : window);
