// Tiny API client — same-origin relative paths (Hub serves this SPA), bearer token in localStorage.

const TOKEN_KEY = "storm_hub_token";

export const auth = {
  get token() {
    return localStorage.getItem(TOKEN_KEY) || "";
  },
  set(token) {
    localStorage.setItem(TOKEN_KEY, token);
  },
  clear() {
    localStorage.removeItem(TOKEN_KEY);
  },
};

export async function api(path, { method = "GET", body } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (auth.token) headers.Authorization = `Bearer ${auth.token}`;
  const res = await fetch(path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  // An API route the running Hub does not have yet comes back as the SPA's index.html (GET) or a 405
  // (POST) -- e.g. dashboard rebuilt but Hub not restarted. Say that, instead of a JSON parse error.
  const missing = `${method} ${path.split("?")[0]} is not served by the running Hub — `
    + `it may need a restart to load newer code`;
  if ((res.headers.get("content-type") || "").includes("text/html")) throw new Error(missing);
  let data = null;
  try { data = text ? JSON.parse(text) : null; }
  catch { throw new Error(`${res.status}: unexpected non-JSON response from ${path.split("?")[0]}`); }
  if (res.status === 405) throw new Error(missing);
  if (!res.ok) {
    const detail = data && data.detail ? data.detail : res.statusText;
    throw new Error(`${res.status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
  }
  return data;
}

// Downloads (e.g. a booked PDF) need the bearer token too, so a plain <a href> won't work --
// fetch as a blob and open it in a new tab.
export async function openFile(path) {
  const headers = {};
  if (auth.token) headers.Authorization = `Bearer ${auth.token}`;
  const res = await fetch(path, { headers });
  if (!res.ok) throw new Error(`${res.status}: could not fetch file`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  window.open(url, "_blank");
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}
