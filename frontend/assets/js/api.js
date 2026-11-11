// API client: bearer access token (sessionStorage, short-lived) + rotating httpOnly refresh cookie.
const cfg = window.PDFMASTER_CONFIG || {};
export const API_BASE = (cfg.apiBase || "").replace(/\/+$/, "");
const TOKEN_KEY = "pdfmaster.access";
const CSRF = { "X-Requested-With": "PDFMasterPro" };
let accessToken = sessionStorage.getItem(TOKEN_KEY);
let refreshing = null;

export class ApiError extends Error {
  constructor(status, code, message, details) {
    super(message);
    Object.assign(this, { status, code, details });
  }
}

function setToken(token) {
  accessToken = token || null;
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
}

async function toError(res) {
  try {
    const { error } = await res.json();
    return new ApiError(res.status, error.code, error.message, error.details);
  } catch {
    return new ApiError(res.status, "http_error", `Request failed (${res.status})`);
  }
}

export function refreshSession() {
  if (!refreshing) {
    refreshing = (async () => {
      const res = await fetch(`${API_BASE}/api/auth/refresh`, { method: "POST", credentials: "include", headers: CSRF });
      if (!res.ok) { setToken(null); return null; }
      const data = await res.json();
      setToken(data.access_token);
      return data.user;
    })().finally(() => { refreshing = null; });
  }
  return refreshing;
}

export function goToLogin() {
  setToken(null);
  if (!/index\.html$|\/$/.test(location.pathname)) location.replace("index.html");
}

export async function api(path, { method = "GET", json, body, raw = false } = {}) {
  const send = () => fetch(API_BASE + path, {
    method, credentials: "include", body: json !== undefined ? JSON.stringify(json) : body,
    headers: { ...CSRF, ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
               ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}) },
  });
  let res = await send();
  if (res.status === 401 && !path.startsWith("/api/auth/")) {
    if (await refreshSession()) res = await send();
    else { goToLogin(); throw new ApiError(401, "not_authenticated", "Please sign in again"); }
  }
  if (!res.ok) throw await toError(res);
  if (raw) return res;
  return res.status === 204 ? null : res.json();
}

export async function login(email, password) {
  const data = await api("/api/auth/login", { method: "POST", json: { email, password } });
  setToken(data.access_token);
  return data.user;
}

export async function register(full_name, email, password) {
  const data = await api("/api/auth/register", { method: "POST", json: { full_name, email, password } });
  setToken(data.access_token);
  return data.user;
}

export async function logout() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch { /* session is cleared locally regardless */ }
  setToken(null);
}

// Resolve the signed-in user, or null. Uses the refresh cookie when this tab has no access token yet.
export async function currentUser() {
  if (!accessToken && !(await refreshSession())) return null;
  try { return await api("/api/auth/me"); }
  catch (e) {
    if (e.status !== 401) throw e;
    return (await refreshSession()) ? api("/api/auth/me") : null;
  }
}

export async function requireUser() {
  const user = await currentUser();
  if (!user) { goToLogin(); return new Promise(() => {}); }
  return user;
}

// Upload with progress reporting (fetch cannot report upload progress).
export function uploadPdf(file, onProgress) {
  const attempt = () => new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE}/api/documents/upload`);
    xhr.withCredentials = true;
    xhr.setRequestHeader("X-Requested-With", CSRF["X-Requested-With"]);
    if (accessToken) xhr.setRequestHeader("Authorization", `Bearer ${accessToken}`);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
    xhr.onerror = () => reject(new ApiError(0, "network_error", "Network error during upload"));
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { /* handled below */ }
      if (xhr.status >= 200 && xhr.status < 300 && data) resolve(data);
      else reject(new ApiError(xhr.status, data?.error?.code || "http_error", data?.error?.message || `Upload failed (${xhr.status})`));
    };
    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  });
  return attempt().catch(async (e) => {
    if (e.status === 401 && (await refreshSession())) return attempt();
    if (e.status === 401) goToLogin();
    throw e;
  });
}

export function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB"];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
  return `${n.toFixed(n < 10 ? 1 : 0)} ${units[i]}`;
}

export function toast(message, kind = "error") {
  let host = document.getElementById("toasts");
  if (!host) { host = Object.assign(document.createElement("div"), { id: "toasts" }); host.setAttribute("aria-live", "polite"); document.body.append(host); }
  const el = Object.assign(document.createElement("div"), { className: `pm-toast pm-toast-${kind}`, textContent: message });
  host.append(el);
  setTimeout(() => el.remove(), kind === "error" ? 7000 : 3500);
  return el;
}
