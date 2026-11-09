// Thin API client. Errors carry a user-safe message and the HTTP status.

export class ApiError extends Error {
  constructor(message, status = 0, code = "") {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function parseError(res) {
  let msg = `Request failed (${res.status}).`;
  let code = "";
  try {
    const body = await res.json();
    if (body?.error) msg = body.error;
    code = body?.code || "";
  } catch { /* not JSON */ }
  if (res.status === 413 && !code) msg = "That request was too large.";
  if (res.status >= 500 && res.status !== 502 && res.status !== 503) msg = "The server had a problem. Please try again.";
  return new ApiError(msg, res.status, code || (res.status === 429 ? "rate_limited" : ""));
}

export async function api(path, { method = "GET", body, signal, params } = {}) {
  const url = new URL(path, location.origin);
  if (params) Object.entries(params).forEach(([k, v]) => v != null && url.searchParams.set(k, v));
  let res;
  try {
    res = await fetch(url, {
      method,
      signal,
      headers: body ? { "Content-Type": "application/json", Accept: "application/json" } : { Accept: "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError(navigator.onLine ? "Couldn't reach the server." : "You're offline.", 0, "network");
  }
  if (!res.ok) throw await parseError(res);
  return res.json();
}

/** POST /api/chat and dispatch Server-Sent Events to `on[event](data)`. */
export async function streamChat(body, on, signal) {
  let res;
  try {
    res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(body),
      signal,
    });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError(navigator.onLine ? "Couldn't reach the server." : "You're offline.", 0, "network");
  }
  if (!res.ok) throw await parseError(res);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let sawDone = false;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let event = "message";
      const data = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      if (!data.length) continue;
      let payload;
      try { payload = JSON.parse(data.join("\n")); } catch { continue; }
      if (event === "done") sawDone = true;
      if (event === "error") throw new ApiError(payload.message || "Something went wrong.", 502, "stream");
      on[event]?.(payload);
    }
  }
  if (!sawDone) throw new ApiError("The connection closed before the answer finished.", 0, "incomplete");
}
