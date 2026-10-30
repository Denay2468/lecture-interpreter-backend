/* Sign in, student sign-up, account activation (lecturer invites and student codes) and password reset. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const api = BLB.api;
  const HOME = { ADMIN: "admin.html", LECTURER: "teacher.html", STUDENT: "student.html" };
  const status = $("status");
  $("api-url").value = api.base();

  function say(text, kind) { status.textContent = text; status.className = "status" + (kind ? " " + kind : ""); }
  function goHome(session) { api.goto(HOME[session.user.role] || "index.html"); }

  function showPanel(name) {
    document.querySelectorAll("[data-panel]").forEach((f) => { f.hidden = f.dataset.panel !== name; });
    document.querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
    say("", "");
    const first = document.querySelector('[data-panel="' + name + '"] input');
    if (first && first.focus) first.focus();
  }
  document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => showPanel(b.dataset.tab)));
  if (location.hash === "#register") showPanel("register");

  // run one action with the submit button disabled and errors shown
  async function run(form, busyText, action) {
    const buttons = form.querySelectorAll("button[type=submit]");
    buttons.forEach((b) => { b.disabled = true; });
    say(busyText, "busy");
    try { await action(); }
    catch (e) { say(e && e.message ? e.message : String(e), "error"); }
    finally { buttons.forEach((b) => { b.disabled = false; }); }
  }

  $("signin").addEventListener("submit", (ev) => {
    ev.preventDefault();
    run($("signin"), "Signing in...", async () => {
      const s = await api.signIn($("si-email").value.trim(), $("si-password").value);
      say("Signed in.", "ok");
      goHome(s);
    });
  });

  $("register").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const email = $("rg-email").value.trim();
    run($("register"), "Sending a code...", async () => {
      await api.register(email, $("rg-name").value);
      showPanel("activate");
      $("ac-email").value = email;
      $("ac-code").focus();
      say("If that address can be registered, we've emailed it a 6-digit code. Enter it below and choose a password.", "ok");
    });
  });

  $("activate").addEventListener("submit", (ev) => {
    ev.preventDefault();
    run($("activate"), "Creating your account...", async () => {
      const s = await api.activate($("ac-email").value.trim(), $("ac-code").value.trim(), $("ac-password").value);
      say("Your account is ready.", "ok");
      goHome(s);
    });
  });

  let resetCodeSent = false;
  $("reset").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const email = $("rs-email").value.trim();
    run($("reset"), resetCodeSent ? "Changing your password..." : "Sending a code...", async () => {
      if (!resetCodeSent) {
        await api.requestReset(email);
        resetCodeSent = true;
        $("reset-step2").hidden = false;
        $("rs-submit").textContent = "Change my password";
        say("If that address has an account, we've emailed it a reset code. Enter it with a new password.", "ok");
        return;
      }
      await api.confirmReset(email, $("rs-code").value.trim(), $("rs-password").value);
      resetCodeSent = false;
      $("reset-step2").hidden = true;
      $("rs-submit").textContent = "Email me a reset code";
      showPanel("signin");
      $("si-email").value = email;
      say("Password changed. Sign in with the new one.", "ok");
    });
  });

  $("api-url").addEventListener("change", () => api.saveBase($("api-url").value.trim()));

  const existing = api.session.get();
  if (existing) goHome(existing); // pages clear a dead session themselves, so this can't loop
})();