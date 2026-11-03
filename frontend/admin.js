/* School admin dashboard: usage, lecturers, rooms, recent lectures, email domains. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const api = BLB.api;
  const session = api.requireRole("ADMIN");
  if (!session) return;

  $("whoami").textContent = session.user.full_name;
  $("signout").addEventListener("click", async () => { await api.signOut(); api.goto("login.html"); });

  function say(el, text, kind) { el.textContent = text; el.className = "status" + (kind ? " " + kind : ""); }
  function cell(tr, text) { const td = document.createElement("td"); td.textContent = text; tr.appendChild(td); return td; }
  function badge(text, kind) { const s = document.createElement("span"); s.className = "badge" + (kind ? " " + kind : ""); s.textContent = text; return s; }
  function button(label, onClick) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "secondary small"; b.textContent = label;
    b.addEventListener("click", onClick);
    return b;
  }
  function fmtDuration(s) {
    if (s == null) return "running";
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? h + " h " + m + " min" : m + " min";
  }
  function fmtDate(iso) { return iso ? new Date(iso).toLocaleString() : ""; }
  function fail(e) {
    if (e && e.status === 401) { api.goto("login.html"); return; }
    say($("page-status"), e && e.message ? e.message : String(e), "error");
  }
  function fillRows(tableId, rows, build) {
    const body = $(tableId).querySelector("tbody");
    body.replaceChildren();
    rows.forEach((r) => { const tr = document.createElement("tr"); build(tr, r); body.appendChild(tr); });
  }

  async function loadUsage() {
    const [me, u] = await Promise.all([api.get("/me"), api.get("/admin/usage")]);
    $("school-name").textContent = me.institution_name;
    const used = (u.used_seconds / 3600).toFixed(1), limit = (u.limit_seconds / 3600).toFixed(0);
    $("usage-text").textContent = used + " of " + limit + " hours used this month (" + (u.remaining_seconds / 3600).toFixed(1) + " h left)";
    $("usage-bar").style.width = Math.min(100, u.limit_seconds ? (100 * u.used_seconds) / u.limit_seconds : 100) + "%";
  }

  async function loadLecturers() {
    const rows = await api.get("/admin/lecturers");
    fillRows("lecturers", rows, (tr, l) => {
      cell(tr, l.full_name); cell(tr, l.email);
      const status = cell(tr, "");
      status.appendChild(!l.is_active ? badge("Deactivated", "err") : l.email_verified_at ? badge("Active", "ok") : badge("Invited", "warn"));
      const actions = cell(tr, "");
      if (l.is_active && !l.email_verified_at) {
        actions.appendChild(button("Resend invite", async () => {
          try { await api.post("/admin/lecturers/" + l.id + "/resend-invite"); say($("invite-status"), "Invite sent to " + l.email + ".", "ok"); }
          catch (e) { say($("invite-status"), e.message, "error"); }
        }));
      }
      actions.appendChild(button(l.is_active ? "Deactivate" : "Reactivate", async () => {
        try { await api.patch("/admin/lecturers/" + l.id, { is_active: !l.is_active }); await loadLecturers(); }
        catch (e) { say($("invite-status"), e.message, "error"); }
      }));
    });
  }

  async function loadRooms() {
    const rows = await api.get("/rooms");
    fillRows("rooms", rows, (tr, r) => {
      cell(tr, r.room_code + (r.room_name ? " - " + r.room_name : ""));
      cell(tr, r.lecturer_name || "");
      cell(tr, r.join_code).className = "join-code";
      cell(tr, "").appendChild(r.is_live ? badge("Live", "ok") : badge("Idle", ""));
      cell(tr, "").appendChild(button("New code", async () => {
        try { await api.post("/rooms/" + r.id + "/rotate-code"); await loadRooms(); } catch (e) { fail(e); }
      }));
    });
  }

  async function loadSessions() {
    const rows = await api.get("/admin/sessions?limit=25");
    fillRows("sessions", rows, (tr, s) => {
      cell(tr, s.room_code); cell(tr, s.lecturer_name); cell(tr, fmtDate(s.started_at)); cell(tr, fmtDuration(s.duration_seconds));
      cell(tr, "").appendChild(badge(s.status === "LIVE" ? "Live" : s.status === "COMPLETED" ? "Completed" : "Interrupted", s.status === "LIVE" ? "ok" : s.status === "ABORTED" ? "warn" : ""));
    });
  }

  async function loadDomains() {
    const rows = await api.get("/admin/email-domains");
    const ul = $("domains");
    ul.replaceChildren();
    rows.forEach((d) => {
      const li = document.createElement("li");
      li.textContent = d.domain + " (" + [d.allow_staff ? "staff" : null, d.allow_students ? "students" : null].filter(Boolean).join(" and ") + ")";
      ul.appendChild(li);
    });
  }

  $("invite-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const name = $("inv-name").value, email = $("inv-email").value.trim();
    say($("invite-status"), "Sending invite...", "busy");
    try {
      await api.post("/admin/lecturers", { email, full_name: name });
      $("inv-name").value = ""; $("inv-email").value = "";
      say($("invite-status"), "Invite sent to " + email + ". They'll get a code to set their password.", "ok");
      await loadLecturers();
    } catch (e) { say($("invite-status"), e.message, "error"); }
  });

  Promise.all([loadUsage(), loadLecturers(), loadRooms(), loadSessions(), loadDomains()]).catch(fail);
})();