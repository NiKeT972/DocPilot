// DocPilot front-end controller (vanilla JS, no build step).
import { api, ApiError, streamChat } from "./api.js";
import { clearSession as clearFiles, deleteFile, saveFile } from "./files.js";
import { ingestFile } from "./ingest.js";
import { escapeHtml, renderMarkdown } from "./md.js";
import { closeViewer, forgetDoc, initViewer, isViewerOpen, openViewer } from "./viewer.js";

// ------------------------------------------------------------------ utils
const $ = (id) => document.getElementById(id);
const LS = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* quota */ } },
  del(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } },
};
const uid = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`);
const fmtInt = (n) => Number(n || 0).toLocaleString("en-US");
const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const ICON = {
  check: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4 4L19 7"/></svg>',
  warn: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 9v4m0 4h.01M10.3 3.9 2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>',
  info: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/></svg>',
  trash: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M10 11v6m4-6v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/></svg>',
  copy: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/></svg>',
  redo: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/></svg>',
  up: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 11v9H4v-9zm0 0 4-7a2 2 0 0 1 3 2l-1 4h5a2 2 0 0 1 2 2.3l-1.2 6A2 2 0 0 1 16.8 20H7"/></svg>',
  down: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17 13V4h3v9zm0 0-4 7a2 2 0 0 1-3-2l1-4H6a2 2 0 0 1-2-2.3l1.2-6A2 2 0 0 1 7.2 4H17"/></svg>',
  pdf: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/></svg>',
  web: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/></svg>',
  both: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 3h6l4 4v8H5z"/><circle cx="16.5" cy="16.5" r="4.5"/></svg>',
  none: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5M9 9l4 4m0-4-4 4"/></svg>',
  chev: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>',
  close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>',
};
const ROUTES = {
  pdf: { label: "From your PDFs", cls: "route-pdf", icon: ICON.pdf },
  web: { label: "From the web", cls: "route-web", icon: ICON.web },
  hybrid: { label: "PDFs + web", cls: "route-hybrid", icon: ICON.both },
  none: { label: "Not found", cls: "route-none", icon: ICON.none },
};

function srSay(text) {
  const n = $("sr-status");
  n.textContent = "";
  setTimeout(() => { n.textContent = text; }, 30);
}

function toast(message, kind = "info", ms = 6000) {
  const t = document.createElement("div");
  t.className = `toast toast-${kind}`;
  t.setAttribute("role", kind === "error" ? "alert" : "status");
  t.innerHTML = `${kind === "info" ? ICON.info : ICON.warn}<span></span><button type="button" aria-label="Dismiss">${ICON.close}</button>`;
  t.querySelector("span").textContent = message;
  t.querySelector("button").onclick = () => t.remove();
  $("toasts").append(t);
  setTimeout(() => t.remove(), ms);
}

function confirmDialog(title, body, okLabel = "Confirm") {
  const d = $("confirm-dialog");
  $("confirm-title").textContent = title;
  $("confirm-body").textContent = body;
  $("confirm-ok").textContent = okLabel;
  return new Promise((resolve) => {
    d.addEventListener("close", () => resolve(d.returnValue === "ok"), { once: true });
    d.returnValue = "cancel";
    d.showModal();
  });
}

function handleError(e, fallback = "Something went wrong.") {
  if (e?.name === "AbortError") return;
  if (e?.status === 429) toast(e.message, "warn", 9000);
  else toast(e?.message || fallback, "error");
}

// ------------------------------------------------------------------ state
let sessionId = LS.get("docpilot.session", null);
if (!sessionId || !/^[A-Za-z0-9-]{8,64}$/.test(sessionId)) {
  sessionId = uid();
  LS.set("docpilot.session", sessionId);
}
const S = {
  health: null,
  docs: [], // {key,id,name,pages,chunks,ocr_pages,status,...}
  messages: LS.get(`docpilot.chat.${sessionId}`, []),
  feedback: LS.get("docpilot.feedback", {}),
  deselected: new Set(LS.get(`docpilot.deselected.${sessionId}`, [])),
  web: LS.get("docpilot.web", true),
  streaming: null, // {controller, msgId}
  uploadQueue: Promise.resolve(),
  suggestions: [],
};
const DEFAULT_LIMITS = {
  max_pdf_mb: 50, max_pages: 1000, max_files_per_session: 10, max_pages_per_session: 2000, max_ocr_pages_per_file: 30,
  max_batch_pages: 40, max_batch_bytes: 2_500_000,
};
const limits = () => ({ ...DEFAULT_LIMITS, ...(S.health?.limits || {}) });
const webAvailable = () => !!S.health?.web_enabled;
const webOn = () => webAvailable() && S.web;
const readyDocs = () => S.docs.filter((d) => d.status === "ready");
const saveMessages = () => LS.set(`docpilot.chat.${sessionId}`, S.messages.filter((m) => m.status !== "streaming").slice(-60));

// ------------------------------------------------------------------ health / banners
async function loadHealth() {
  try {
    S.health = await api("/api/health");
  } catch {
    S.health = null;
    renderBanners();
    return;
  }
  const l = limits();
  $("limits-text").textContent = `Up to ${l.max_files_per_session} PDFs · ${l.max_pdf_mb} MB · ${fmtInt(l.max_pages)} pages each`;
  const toggle = $("web-toggle");
  toggle.checked = webOn();
  toggle.disabled = !webAvailable();
  $("web-toggle-row").querySelector(".switch").classList.toggle("disabled", !webAvailable());
  $("web-hint").hidden = webAvailable();
  if (!webAvailable()) $("web-toggle-row").title = "Web search isn't configured on this server (no TAVILY_API_KEY).";
  const m = S.health.models || {};
  $("topbar-meta").innerHTML =
    `<span class="meta-pill model" title="Answer model">${escapeHtml(m.answer || "")}</span>` +
    `<span class="meta-pill"><span class="meta-dot${webOn() ? "" : " off"}"></span>Web ${webOn() ? "on" : "off"}</span>`;
  renderBanners();
  updateComposer();
}

function renderBanners() {
  const box = $("banners");
  box.replaceChildren();
  const add = (cls, icon, html) => {
    const b = document.createElement("div");
    b.className = `banner ${cls}`;
    b.setAttribute("role", cls === "banner-error" ? "alert" : "status");
    b.innerHTML = `${icon}<div>${html}</div>`;
    box.append(b);
  };
  if (!navigator.onLine) add("banner-warn", ICON.warn, "<strong>You're offline.</strong> Uploads and questions will work again once you reconnect.");
  else if (!S.health) add("banner-error", ICON.warn, "<strong>Can't reach the DocPilot server.</strong> Check your connection and reload.");
  else if (!S.health.configured) {
    add("banner-error", ICON.warn, `<strong>Server not configured.</strong> Missing ${S.health.missing.map((k) => `<code>${escapeHtml(k)}</code>`).join(", ")}. Add the keys to the environment and redeploy.`);
  }
}

// ------------------------------------------------------------------ documents
function docStatusChip(d) {
  switch (d.status) {
    case "queued": return `<span class="chip chip-busy">Waiting…</span>`;
    case "reading": return `<span class="chip chip-busy">Reading ${fmtInt(d.done)}/${fmtInt(d.total)}</span>`;
    case "ocr": return `<span class="chip chip-ocr">OCR p.${d.ocrPage}</span>`;
    case "indexing": return `<span class="chip chip-busy">Indexing ${d.pct || 0}%</span>`;
    case "retrying": return `<span class="chip chip-retry">Retrying batch…</span>`;
    case "failed": return `<span class="chip chip-failed">Failed</span>`;
    default:
      return `<span class="chip chip-ready">Ready</span>${d.ocr_pages ? ` <span class="chip chip-ocr" title="${d.ocr_pages} scanned pages were read with OCR">OCR used</span>` : ""}`;
  }
}

function renderDocs() {
  const list = $("doc-list");
  list.replaceChildren();
  for (const d of S.docs) {
    const li = document.createElement("li");
    li.className = "doc";
    const busy = !["ready", "failed"].includes(d.status);
    const checkId = `chk-${d.key}`;
    const selectable = d.status === "ready";
    li.innerHTML = `
      <input type="checkbox" class="doc-check" id="${checkId}" ${selectable && !S.deselected.has(d.id) ? "checked" : ""} ${selectable ? "" : "disabled"}
        aria-label="Search in ${escapeHtml(d.name)}" title="Search in this doc">
      <div class="doc-main">
        <button type="button" class="doc-name" title="${escapeHtml(d.name)}">${escapeHtml(d.name)}</button>
        <div class="doc-meta">${d.pages ? `<span>${fmtInt(d.pages)} pages</span>` : ""}${docStatusChip(d)}</div>
      </div>
      <button type="button" class="icon-btn doc-del" aria-label="Remove ${escapeHtml(d.name)}" ${busy ? "disabled" : ""}>${ICON.trash}</button>`;
    if (busy) {
      const pct = d.status === "reading" || d.status === "ocr" ? Math.round(((d.done || 0) / (d.total || 1)) * 50) : 50 + Math.round((d.pct || 0) / 2);
      li.insertAdjacentHTML("beforeend", `<div class="progress" role="progressbar" aria-label="Processing ${escapeHtml(d.name)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><span style="width:${pct}%"></span></div>`);
    }
    if (d.status === "failed") {
      const err = document.createElement("div");
      err.className = "doc-error";
      err.innerHTML = `<span></span>${d.file ? '<button type="button">Retry</button>' : ""}`;
      err.querySelector("span").textContent = d.error || "Upload failed.";
      err.querySelector("button")?.addEventListener("click", () => retryUpload(d));
      li.append(err);
    }
    li.querySelector(".doc-check").addEventListener("change", (e) => toggleSelect(d, e.target));
    li.querySelector(".doc-name").addEventListener("click", () => {
      if (d.id && d.status === "ready") { openViewer({ docId: d.id, docName: d.name, page: 1 }); closeDrawer(); }
    });
    li.querySelector(".doc-del").addEventListener("click", () => removeDoc(d));
    list.append(li);
  }
  const n = S.docs.length;
  $("doc-count").textContent = n;
  $("doc-count").setAttribute("aria-label", `${n} document${n === 1 ? "" : "s"}`);
  $("docs-empty").hidden = n > 0;
  $("dropzone").classList.toggle("disabled", n >= limits().max_files_per_session);
  updateComposer();
}

function toggleSelect(d, input) {
  if (!input.checked) {
    const remaining = readyDocs().filter((x) => x.id !== d.id && !S.deselected.has(x.id));
    if (!remaining.length) {
      input.checked = true;
      toast("At least one document must be searched.", "info", 3500);
      return;
    }
    S.deselected.add(d.id);
  } else S.deselected.delete(d.id);
  LS.set(`docpilot.deselected.${sessionId}`, [...S.deselected]);
  updateComposer();
}

async function refreshDocs() {
  try {
    const { documents } = await api("/api/documents", { params: { session_id: sessionId } });
    const busy = S.docs.filter((d) => !["ready"].includes(d.status) && !documents.some((x) => x.id === d.id));
    S.docs = [...documents.map((x) => ({ ...x, key: x.id, status: "ready" })), ...busy];
    renderDocs();
  } catch (e) {
    if (S.health) handleError(e, "Couldn't load your documents.");
  }
}

async function removeDoc(d) {
  if (d.status === "failed" && !d.id) {
    S.docs = S.docs.filter((x) => x !== d);
    renderDocs();
    return;
  }
  const ok = await confirmDialog("Remove document?", `“${d.name}” and its index will be deleted from this chat.`, "Remove");
  if (!ok) return;
  try {
    if (d.id) await api(`/api/documents/${encodeURIComponent(d.id)}`, { method: "DELETE", params: { session_id: sessionId } });
  } catch (e) {
    if (e.status !== 404) return handleError(e);
  }
  if (d.id) { await deleteFile(d.id); forgetDoc(d.id); S.deselected.delete(d.id); }
  S.docs = S.docs.filter((x) => x !== d);
  renderDocs();
  srSay(`${d.name} removed`);
  if (!readyDocs().length) { S.suggestions = []; renderSuggestions(); }
}

function addFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  const l = limits();
  for (const file of files) {
    const isPdf = file.type === "application/pdf" || /\.pdf$/i.test(file.name);
    if (!isPdf) { toast(`“${file.name}” isn't a PDF.`, "warn"); continue; }
    if (file.size > l.max_pdf_mb * 1024 * 1024) { toast(`“${file.name}” is larger than ${l.max_pdf_mb} MB.`, "warn"); continue; }
    if (S.docs.filter((d) => d.status !== "failed").length >= l.max_files_per_session) {
      toast(`You can add up to ${l.max_files_per_session} documents per chat.`, "warn");
      break;
    }
    const entry = { key: uid(), id: null, name: file.name, pages: 0, status: "queued", file };
    S.docs.push(entry);
    S.uploadQueue = S.uploadQueue.then(() => runUpload(entry));
  }
  renderDocs();
}

function retryUpload(d) {
  d.status = "queued";
  d.error = "";
  renderDocs();
  S.uploadQueue = S.uploadQueue.then(() => runUpload(d));
}

async function runUpload(entry) {
  if (!S.health?.configured) {
    entry.status = "failed";
    entry.error = "Server isn't configured yet.";
    renderDocs();
    return;
  }
  let lastRender = 0;
  const update = (force = false) => {
    const now = performance.now();
    if (force || now - lastRender > 120) { lastRender = now; renderDocs(); }
  };
  try {
    const buffer = await entry.file.arrayBuffer();
    const result = await ingestFile({
      file: entry.file,
      buffer,
      sessionId,
      limits: limits(),
      ocrEnabled: !!S.health?.ocr_enabled,
      cb: (p) => {
        if (p.phase === "reading") { entry.status = "reading"; entry.done = p.done; entry.total = p.total; entry.pages = p.total; }
        else if (p.phase === "ocr") { entry.status = "ocr"; entry.ocrPage = p.page; entry.done = p.done; entry.total = p.total; }
        else if (p.phase === "ocr-off") toast(p.message, "warn", 8000);
        else if (p.phase === "indexing") { entry.status = "indexing"; entry.pct = p.pct; }
        else if (p.phase === "retrying") { entry.status = "retrying"; update(true); return; }
        else if (p.phase === "created") {
          entry.id = p.docId;
          if (p.replaced) {
            S.docs = S.docs.filter((d) => d.id !== p.replaced || d === entry);
            deleteFile(p.replaced);
            forgetDoc(p.replaced);
            toast(`Replaced the earlier copy of “${entry.name}”.`, "info", 4000);
          }
        }
        update();
      },
    });
    await saveFile(sessionId, result.doc_id, entry.name, new Blob([buffer], { type: "application/pdf" }));
    Object.assign(entry, { id: result.doc_id, key: result.doc_id, status: "ready", chunks: result.chunks, ocr_pages: result.ocr_pages, file: null });
    renderDocs();
    srSay(`${entry.name} is ready`);
    if (result.warning) toast(`${entry.name}: ${result.warning}`, "warn", 9000);
    else if (result.ocrUsed) toast(`${entry.name}: ${result.ocrUsed} scanned page${result.ocrUsed > 1 ? "s were" : " was"} read with OCR.`, "info");
    if (result.ocrSkipped) toast(`${entry.name}: ${result.ocrSkipped} page${result.ocrSkipped > 1 ? "s have" : " has"} no text and couldn't be OCR'd (limit reached).`, "warn", 9000);
    loadSuggestions(result.doc_id);
  } catch (e) {
    entry.status = "failed";
    entry.error = e?.message || "Upload failed.";
    if (e?.name === "PasswordException") entry.error = "This PDF is password-protected.";
    else if (e?.name === "InvalidPDFException") entry.error = "This file isn't a valid PDF.";
    if (e?.status === 429) toast(e.message, "warn", 9000);
    if (entry.id) {
      // Partially indexed: remove the server copy so the index never has half a document.
      api(`/api/documents/${encodeURIComponent(entry.id)}`, { method: "DELETE", params: { session_id: sessionId } }).catch(() => {});
      entry.id = null;
    }
    renderDocs();
    srSay(`${entry.name} failed: ${entry.error}`);
  }
}

// ------------------------------------------------------------------ suggestions / empty state
async function loadSuggestions(docId) {
  S.suggestions = null; // loading
  renderSuggestions();
  try {
    const res = await api("/api/suggest", { method: "POST", body: { session_id: sessionId, doc_id: docId } });
    S.suggestions = res.questions || [];
  } catch {
    S.suggestions = [];
  }
  renderSuggestions();
}

function renderSuggestions() {
  const box = $("suggestions");
  if (!box) return;
  box.replaceChildren();
  if (S.suggestions === null) {
    box.innerHTML = `<div class="suggestions-label">Preparing suggested questions…</div>` +
      '<span class="suggestion skeleton" aria-hidden="true">.</span>'.repeat(3);
    return;
  }
  if (!S.suggestions.length) return;
  const label = document.createElement("div");
  label.className = "suggestions-label";
  label.textContent = "Try asking";
  box.append(label);
  for (const q of S.suggestions) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "suggestion";
    b.textContent = q;
    b.addEventListener("click", () => ask(q));
    box.append(b);
  }
}

function renderEmpty() {
  const box = $("messages");
  if (S.messages.length) return;
  box.replaceChildren($("empty-tpl").content.cloneNode(true));
  renderSuggestions();
}

// ------------------------------------------------------------------ messages
function routeBadge(route, status) {
  if (status === "stopped") return `<span class="route-badge route-none">${ICON.none}Stopped</span>`;
  if (!route) return `<span class="route-badge route-working"><span class="spinner" aria-hidden="true"></span>Working…</span>`;
  const r = ROUTES[route] || ROUTES.none;
  return `<span class="route-badge ${r.cls}">${r.icon}${r.label}</span>`;
}

function timelineHtml(steps, live) {
  return steps.map((s) => {
    const icon = s.status === "start" && live
      ? '<span class="spinner" aria-hidden="true"></span>'
      : s.failed ? ICON.warn : ICON.check;
    const ms = s.ms != null ? `<span class="tl-ms">${s.ms < 1000 ? `${s.ms} ms` : `${(s.ms / 1000).toFixed(1)} s`}</span>` : "";
    return `<li class="tl-item ${s.status === "end" ? (s.failed ? "failed" : "done") : ""}"><span class="tl-icon">${icon}</span><span>${escapeHtml(s.label)}</span>${ms}</li>`;
  }).join("");
}

function sourcesHtml(sources) {
  if (!sources?.length) return "";
  return `<div class="sources" role="group" aria-label="Sources">${sources.map((s) => {
    if (s.type === "pdf") {
      return `<button type="button" class="source" data-tag="${s.tag}">
        <span class="source-top"><span class="cite cite-pdf">${s.tag}</span><span class="source-title">${escapeHtml(s.doc_name)} · p.${s.page}</span></span>
        <span class="source-snip">${escapeHtml(s.snippet)}</span></button>`;
    }
    let host = s.domain || "";
    try { host = host || new URL(s.url).hostname.replace(/^www\./, ""); } catch { /* ignore */ }
    return `<a class="source" data-tag="${s.tag}" href="${escapeHtml(s.url)}" target="_blank" rel="noopener noreferrer nofollow">
      <span class="source-top"><span class="cite cite-web">${s.tag}</span><img class="favicon" alt="" width="16" height="16" loading="lazy" referrerpolicy="no-referrer" src="https://icons.duckduckgo.com/ip3/${encodeURIComponent(host)}.ico"><span class="source-title">${escapeHtml(s.title)}</span></span>
      <span class="source-snip">${escapeHtml(host)} — ${escapeHtml(s.snippet)}</span></a>`;
  }).join("")}</div>`;
}

function renderUser(m) {
  const wrap = document.createElement("div");
  wrap.className = "msg-user";
  wrap.innerHTML = `<div class="bubble-user"></div>`;
  wrap.firstChild.textContent = m.content;
  return wrap;
}

function renderBot(m) {
  const wrap = document.createElement("article");
  wrap.className = "msg-bot";
  wrap.dataset.id = m.id;
  wrap.setAttribute("aria-label", "DocPilot answer");
  if (m.status === "error") {
    wrap.innerHTML = `<div class="card error-card" role="alert">${ICON.warn}<div><strong>Couldn't answer.</strong><div class="err-text"></div></div><button type="button" class="btn btn-secondary">Try again</button></div>`;
    wrap.querySelector(".err-text").textContent = m.error || "Something went wrong.";
    wrap.querySelector("button").addEventListener("click", () => regenerate(m.id));
    return wrap;
  }
  const live = m.status === "streaming";
  const steps = m.steps || [];
  const latency = m.latency ? `<span class="latency">${(m.latency / 1000).toFixed(1)} s</span>` : "";
  const timeline = live
    ? `<ol class="timeline" aria-label="Agent progress">${timelineHtml(steps, true)}</ol>`
    : steps.length
      ? `<details class="how"><summary>${ICON.chev}How I got this answer</summary><ol class="timeline">${timelineHtml(steps, false)}</ol></details>`
      : "";
  const answer = m.content ? renderMarkdown(m.content) : "";
  const caret = live ? '<span class="caret" aria-hidden="true"></span>' : "";
  const fb = S.feedback[m.id];
  const actions = live ? "" : `<div class="actions">
      <button type="button" class="icon-btn" data-act="copy" aria-label="Copy answer" title="Copy answer">${ICON.copy}</button>
      <button type="button" class="icon-btn" data-act="regen" aria-label="Regenerate answer" title="Regenerate">${ICON.redo}</button>
      <button type="button" class="icon-btn" data-act="up" aria-label="Good answer" aria-pressed="${fb === "up"}" title="Good answer">${ICON.up}</button>
      <button type="button" class="icon-btn" data-act="down" aria-label="Bad answer" aria-pressed="${fb === "down"}" title="Bad answer">${ICON.down}</button>
    </div>`;
  wrap.innerHTML = `<div class="card">
      <div class="card-head">${routeBadge(live ? null : m.route, m.status)}${latency}</div>
      ${timeline}
      <div class="answer">${answer}${caret}</div>
      ${live ? "" : sourcesHtml(m.sources)}
      ${actions}
    </div>`;
  // Put the caret inside the last block so it sits at the end of the text.
  const ans = wrap.querySelector(".answer");
  const caretEl = ans.querySelector(".caret");
  const lastBlock = caretEl && ans.children.length > 1 ? ans.children[ans.children.length - 2] : null;
  if (caretEl && lastBlock && !["UL", "OL", "DIV", "PRE"].includes(lastBlock.tagName)) lastBlock.append(caretEl);
  wrap.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => onAction(m.id, b.dataset.act, b)));
  return wrap;
}

function renderMessages() {
  const box = $("messages");
  if (!S.messages.length) { renderEmpty(); return; }
  box.replaceChildren(...S.messages.map((m) => (m.role === "user" ? renderUser(m) : renderBot(m))));
}

function patchMessage(m) {
  const old = document.querySelector(`.msg-bot[data-id="${CSS.escape(m.id)}"]`);
  const fresh = renderBot(m);
  if (old) old.replaceWith(fresh); else $("messages").append(fresh);
}

function scrollToBottom(force = false) {
  const chat = $("chat");
  const near = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 160;
  if (force || near) chat.scrollTo({ top: chat.scrollHeight, behavior: force && !reduceMotion() ? "smooth" : "auto" });
}

// ------------------------------------------------------------------ chat
function buildHistory(beforeIndex) {
  const prior = S.messages.slice(0, beforeIndex).filter((m) => m.content && (m.role === "user" || m.status === "done"));
  return prior.slice(-6).map((m) => ({ role: m.role, content: m.content.slice(0, 3800) }));
}

function selectedDocIds() {
  const ready = readyDocs();
  const chosen = ready.filter((d) => !S.deselected.has(d.id)).map((d) => d.id);
  return chosen.length && chosen.length < ready.length ? chosen : null;
}

async function ask(question, { replaceId = null } = {}) {
  question = question.trim();
  if (!question || S.streaming || !canAsk()) return;
  let botIndex;
  let bot;
  if (replaceId) {
    botIndex = S.messages.findIndex((m) => m.id === replaceId);
    bot = { id: uid(), role: "assistant", content: "", status: "streaming", steps: [], question };
    S.messages[botIndex] = bot;
  } else {
    if (!S.messages.length) $("messages").replaceChildren();
    const user = { id: uid(), role: "user", content: question };
    S.messages.push(user);
    $("messages").append(renderUser(user));
    bot = { id: uid(), role: "assistant", content: "", status: "streaming", steps: [], question };
    S.messages.push(bot);
    botIndex = S.messages.length - 1;
  }
  patchMessage(bot);
  scrollToBottom(true);
  const history = buildHistory(botIndex - 1);
  const controller = new AbortController();
  S.streaming = { controller, msgId: bot.id };
  updateComposer();
  srSay("Working on your answer");

  let raf = 0;
  const schedule = () => {
    if (raf) return;
    raf = requestAnimationFrame(() => { raf = 0; patchMessage(bot); scrollToBottom(); });
  };
  try {
    await streamChat(
      { session_id: sessionId, question, history, allow_web: webOn(), doc_ids: selectedDocIds() },
      {
        step(ev) {
          if (ev.status === "start") bot.steps.push({ node: ev.node, label: ev.label, status: "start" });
          else {
            const s = [...bot.steps].reverse().find((x) => x.node === ev.node && x.status === "start");
            if (s) Object.assign(s, { label: ev.label, status: "end", ms: ev.ms, failed: !!ev.failed });
            else bot.steps.push({ ...ev });
            srSay(ev.label);
          }
          schedule();
        },
        token(ev) { bot.content += ev.text; schedule(); },
        sources(ev) { bot.sources = ev.sources; },
        done(ev) {
          bot.content = ev.answer || bot.content;
          bot.route = ev.route;
          bot.latency = ev.latency_ms;
          bot.status = "done";
          bot.steps = (ev.trace || []).map((t) => ({ ...t, status: "end" }));
          if ((ev.warnings || []).includes("web_search")) toast("Web search failed for this question, so the answer uses your PDFs only.", "warn");
        },
      },
      controller.signal,
    );
    cancelAnimationFrame(raf);
    srSay(`Answer ready: ${ROUTES[bot.route]?.label || ""}`);
  } catch (e) {
    cancelAnimationFrame(raf);
    if (e.name === "AbortError") {
      bot.status = bot.content ? "stopped" : "error";
      bot.error = "Stopped before an answer was written.";
      bot.steps = bot.steps.map((s) => ({ ...s, status: "end" }));
      bot.route = bot.content ? undefined : bot.route;
    } else {
      bot.status = "error";
      bot.error = e.message;
    }
    srSay(bot.error || "Stopped");
  } finally {
    if (raf) cancelAnimationFrame(raf);
    S.streaming = null;
    patchMessage(bot);
    saveMessages();
    updateComposer();
    scrollToBottom();
  }
}

function regenerate(id) {
  const i = S.messages.findIndex((m) => m.id === id);
  if (i < 1) return;
  const q = S.messages[i].question || S.messages[i - 1]?.content;
  if (q) ask(q, { replaceId: id });
}

async function onAction(id, act, btn) {
  const m = S.messages.find((x) => x.id === id);
  if (!m) return;
  if (act === "copy") {
    try {
      await navigator.clipboard.writeText(m.content);
      toast("Answer copied.", "info", 2500);
    } catch { toast("Couldn't copy to the clipboard.", "warn"); }
  } else if (act === "regen") regenerate(id);
  else {
    S.feedback[id] = S.feedback[id] === act ? undefined : act;
    LS.set("docpilot.feedback", S.feedback);
    btn.closest(".actions").querySelectorAll('[data-act="up"],[data-act="down"]').forEach((b) =>
      b.setAttribute("aria-pressed", String(S.feedback[id] === b.dataset.act)));
    if (S.feedback[id]) srSay("Thanks for the feedback");
  }
}

// ------------------------------------------------------------------ citations
let hideTipFn = null;
const hideTip = () => hideTipFn?.();

function findSource(el) {
  const card = el.closest(".msg-bot");
  const m = card && S.messages.find((x) => x.id === card.dataset.id);
  return m?.sources?.find((s) => s.tag === el.dataset.tag) || null;
}

function openSource(src) {
  if (!src) return;
  if (src.type === "pdf") {
    openViewer({ docId: src.doc_id, docName: src.doc_name, page: src.page, highlight: src.text || src.snippet });
  } else window.open(src.url, "_blank", "noopener,noreferrer");
}

function initCitations() {
  const box = $("messages");
  box.addEventListener("click", (e) => {
    const el = e.target.closest(".cite[data-tag], button.source[data-tag]");
    if (!el || el.tagName === "A") return;
    const src = findSource(el);
    if (src) { e.preventDefault(); openSource(src); hideTip(); }
    else if (el.classList.contains("cite")) toast("That source is still loading.", "info", 2000);
  });
  const tip = $("tooltip");
  let current = null;
  function show(el) {
    const src = findSource(el);
    if (!src) return;
    current = el;
    const title = src.type === "pdf" ? `${src.doc_name} · p.${src.page}` : `${src.title}`;
    tip.innerHTML = `<strong></strong><span></span>`;
    tip.querySelector("strong").textContent = title;
    tip.querySelector("span").textContent = (src.snippet || "").slice(0, 280) + ((src.snippet || "").length > 280 ? "…" : "");
    tip.hidden = false;
    el.setAttribute("aria-describedby", "tooltip");
    const r = el.getBoundingClientRect();
    const tw = tip.offsetWidth;
    const th = tip.offsetHeight;
    let left = Math.min(window.innerWidth - tw - 8, Math.max(8, r.left + r.width / 2 - tw / 2));
    let top = r.top - th - 8;
    if (top < 8) top = r.bottom + 8;
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
  }
  hideTipFn = () => {
    tip.hidden = true;
    current?.removeAttribute("aria-describedby");
    current = null;
  };
  box.addEventListener("mouseover", (e) => { const el = e.target.closest(".cite[data-tag]"); if (el && el !== current) show(el); });
  box.addEventListener("mouseout", (e) => { if (e.target.closest(".cite[data-tag]")) hideTip(); });
  box.addEventListener("focusin", (e) => { const el = e.target.closest(".cite[data-tag]"); if (el) show(el); });
  box.addEventListener("focusout", (e) => { if (e.target.closest(".cite[data-tag]")) hideTip(); });
  $("chat").addEventListener("scroll", () => hideTip(), { passive: true });
  // Hide favicons that fail to load instead of showing a broken image.
  box.addEventListener("error", (e) => { if (e.target.classList?.contains("favicon")) e.target.remove(); }, true);
}


// ------------------------------------------------------------------ composer
function canAsk() {
  return !!S.health?.configured && navigator.onLine && (readyDocs().length > 0 || webOn());
}

function updateComposer() {
  const input = $("composer-input");
  const hint = $("composer-hint");
  const streaming = !!S.streaming;
  $("send-btn").hidden = streaming;
  $("stop-btn").hidden = !streaming;
  let msg = "Enter to send · Shift+Enter for a new line";
  let warn = false;
  if (!S.health) { msg = "Connecting to the server…"; warn = true; }
  else if (!S.health.configured) { msg = "The server isn't configured yet."; warn = true; }
  else if (!navigator.onLine) { msg = "You're offline."; warn = true; }
  else if (!readyDocs().length && !webOn()) {
    const busy = S.docs.some((d) => !["ready", "failed"].includes(d.status));
    msg = busy ? "Your document is still being indexed…" : "Upload a PDF to start asking questions" + (webAvailable() ? ", or turn on web search." : ".");
    warn = true;
  } else if (!readyDocs().length && webOn()) msg = "No documents yet — answers will come from the web.";
  const enabled = canAsk();
  input.disabled = !enabled && !streaming;
  $("send-btn").disabled = !enabled || streaming || !input.value.trim();
  hint.textContent = msg;
  hint.classList.toggle("warn", warn);
  input.placeholder = enabled ? (readyDocs().length ? "Ask a question about your PDFs…" : "Ask anything…") : "Upload a PDF to begin";
}

function autoGrow() {
  const t = $("composer-input");
  t.style.height = "auto";
  t.style.height = `${Math.min(200, t.scrollHeight)}px`;
}

function initComposer() {
  const input = $("composer-input");
  input.addEventListener("input", () => { autoGrow(); updateComposer(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      $("composer").requestSubmit();
    }
  });
  $("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q || S.streaming || !canAsk()) return;
    input.value = "";
    autoGrow();
    ask(q);
  });
  $("stop-btn").addEventListener("click", () => S.streaming?.controller.abort());
}

// ------------------------------------------------------------------ sidebar / drawer
function openDrawer() {
  $("app").classList.add("drawer-open");
  $("scrim").hidden = false;
  $("menu-btn").setAttribute("aria-expanded", "true");
  $("sidebar-close").focus();
}
function closeDrawer() {
  if (!$("app").classList.contains("drawer-open")) return;
  $("app").classList.remove("drawer-open");
  $("scrim").hidden = true;
  $("menu-btn").setAttribute("aria-expanded", "false");
  $("menu-btn").focus({ preventScroll: true });
}

function initSidebar() {
  const dz = $("dropzone");
  const input = $("file-input");
  dz.addEventListener("click", () => input.click());
  dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
  input.addEventListener("change", () => { addFiles(input.files); input.value = ""; });
  ["dragenter", "dragover"].forEach((t) => document.addEventListener(t, (e) => {
    if (![...(e.dataTransfer?.types || [])].includes("Files")) return;
    e.preventDefault();
    dz.classList.add("dragover");
  }));
  ["dragleave", "drop"].forEach((t) => document.addEventListener(t, (e) => {
    if (t === "dragleave" && e.relatedTarget) return;
    dz.classList.remove("dragover");
  }));
  document.addEventListener("drop", (e) => {
    if (!e.dataTransfer?.files?.length) return;
    e.preventDefault();
    addFiles(e.dataTransfer.files);
  });
  document.addEventListener("paste", (e) => {
    const files = [...(e.clipboardData?.files || [])];
    if (files.length) { e.preventDefault(); addFiles(files); }
  });

  $("web-toggle").addEventListener("change", (e) => {
    S.web = e.target.checked;
    LS.set("docpilot.web", S.web);
    loadHealthMetaOnly();
    updateComposer();
    srSay(`Web search ${S.web ? "on" : "off"}`);
  });

  $("new-chat").addEventListener("click", async () => {
    if (S.streaming) S.streaming.controller.abort();
    const ok = await confirmDialog("Start a new chat?", "This clears the conversation and removes all uploaded documents from this chat.", "New chat");
    if (!ok) return;
    const old = sessionId;
    api("/api/documents", { method: "DELETE", params: { session_id: old } }).catch(() => {});
    await clearFiles(old);
    S.docs.forEach((d) => d.id && forgetDoc(d.id));
    LS.del(`docpilot.chat.${old}`);
    LS.del(`docpilot.deselected.${old}`);
    sessionId = uid();
    LS.set("docpilot.session", sessionId);
    Object.assign(S, { docs: [], messages: [], suggestions: [], deselected: new Set() });
    closeViewer();
    renderDocs();
    renderMessages();
    closeDrawer();
    $("composer-input").focus();
    srSay("New chat started");
  });

  $("menu-btn").addEventListener("click", openDrawer);
  $("sidebar-close").addEventListener("click", closeDrawer);
  $("scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if ($("app").classList.contains("drawer-open")) closeDrawer();
    else if (isViewerOpen() && !$("confirm-dialog").open) closeViewer();
  });
}

function loadHealthMetaOnly() {
  const pill = $("topbar-meta").querySelectorAll(".meta-pill")[1];
  if (pill) pill.innerHTML = `<span class="meta-dot${webOn() ? "" : " off"}"></span>Web ${webOn() ? "on" : "off"}`;
}

// ------------------------------------------------------------------ boot
async function boot() {
  initViewer();
  initSidebar();
  initComposer();
  initCitations();
  window.addEventListener("online", () => { renderBanners(); updateComposer(); if (!S.health) loadHealth(); });
  window.addEventListener("offline", () => { renderBanners(); updateComposer(); });
  // Messages that were mid-stream when the page closed can't be resumed.
  S.messages = S.messages.map((m) => (m.status === "streaming" ? { ...m, status: "error", error: "Interrupted." } : m));
  renderMessages();
  renderDocs();
  await loadHealth();
  await refreshDocs();
  if (readyDocs().length && !S.messages.length) loadSuggestions(readyDocs().at(-1).id);
  updateComposer();
}

boot();
