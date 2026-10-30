/* Client for the BLB HTTP API. Keeps the signed-in session in this browser tab (sessionStorage) and refreshes
   the short-lived access token when it is about to expire or the server says it has. Needs common.js first. */
(function (root) {
  "use strict";
  const BLB = root.BLB;
  const KEY = "blb.session";

  function store() { try { return root.sessionStorage; } catch (e) { return null; } }

  function apiBase() {
    let saved = null;
    try { saved = root.localStorage.getItem("blb.apiUrl"); } catch (e) { /* storage unavailable */ }
    if (saved) return saved;
    const loc = root.location || {};
    return (loc.protocol === "https:" ? "https" : "http") + "://" + (loc.hostname || "localhost") + ":8000";
  }
  function saveBase(url) {
    try { if (url) root.localStorage.setItem("blb.apiUrl", url.replace(/\/+$/, "")); } catch (e) { /* ignore */ }
  }

  const Session = {
    get() {
      const s = store();
      if (!s) return null;
      try { return JSON.parse(s.getItem(KEY) || "null"); } catch (e) { return null; }
    },
    set(t) {
      const rec = { access_token: t.access_token, refresh_token: t.refresh_token, user: t.user, expires_at: Date.now() + t.expires_in * 1000 };
      const s = store();
      if (s) s.setItem(KEY, JSON.stringify(rec));
      return rec;
    },
    clear() { const s = store(); if (s) s.removeItem(KEY); },
  };

  class ApiError extends Error {
    constructor(status, message) { super(message); this.status = status; }
  }

  async function raw(method, path, body, token) {
    const headers = { "Content-Type": "application/json" };
    if (token) headers.Authorization = "Bearer " + token;
    let res;
    try {
      res = await root.fetch(apiBase() + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    } catch (e) {
      throw new ApiError(0, "Couldn't reach the server. Is it running, and is the address right?");
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* 204 or empty body */ }
    if (!res.ok) throw new ApiError(res.status, (data && data.detail) || "Request failed (" + res.status + ").");
    return data;
  }

  let refreshing = null;
  function refreshTokens() {
    if (refreshing) return refreshing; // several requests failing at once share one refresh
    const s = Session.get();
    if (!s) return Promise.reject(new ApiError(401, "Sign in required."));
    refreshing = raw("POST", "/auth/refresh", { refresh_token: s.refresh_token })
      .then((t) => Session.set(t))
      .catch((e) => { if (e.status === 401) Session.clear(); throw e; })
      .finally(() => { refreshing = null; });
    return refreshing;
  }

  async function authed(method, path, body) {
    let s = Session.get();
    if (!s) throw new ApiError(401, "Sign in required.");
    if (Date.now() > s.expires_at - 15000) s = await refreshTokens();
    try {
      return await raw(method, path, body, s.access_token);
    } catch (e) {
      if (e.status !== 401) throw e;
      s = await refreshTokens();
      return raw(method, path, body, s.access_token);
    }
  }

  const api = {
    ApiError, base: apiBase, saveBase, session: Session,
    // BLB_NAVIGATE is a hook for tests; in a browser this is just a redirect
    goto(url) { if (typeof root.BLB_NAVIGATE === "function") root.BLB_NAVIGATE(url); else root.location.href = url; },
    requireRole() {
      const roles = Array.prototype.slice.call(arguments);
      const s = Session.get();
      if (!s || roles.indexOf(s.user.role) < 0) { api.goto("login.html"); return null; }
      return s;
    },
    async accessToken() { // for WebSocket URLs
      let s = Session.get();
      if (!s) return null;
      if (Date.now() > s.expires_at - 15000) {
        try { s = await refreshTokens(); } catch (e) { return null; }
      }
      return s.access_token;
    },
    signIn: async (email, password) => Session.set(await raw("POST", "/auth/login", { email, password })),
    register: (email, full_name) => raw("POST", "/auth/register", { email, full_name }),
    activate: async (email, code, password) => Session.set(await raw("POST", "/auth/activate", { email, code, password })),
    requestReset: (email) => raw("POST", "/auth/password-reset/request", { email }),
    confirmReset: (email, code, password) => raw("POST", "/auth/password-reset/confirm", { email, code, password }),
    async signOut() {
      const s = Session.get();
      Session.clear();
      if (s) { try { await raw("POST", "/auth/logout", { refresh_token: s.refresh_token }); } catch (e) { /* already signed out locally */ } }
    },
    get: (path) => authed("GET", path),
    post: (path, body) => authed("POST", path, body === undefined ? {} : body),
    patch: (path, body) => authed("PATCH", path, body),
  };
  BLB.api = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : window);
