// PDF viewer panel: renders the browser's own copy of the PDF (from IndexedDB),
// jumps to the cited page and highlights the cited passage on the text layer.
import { getFile } from "./files.js";
import { closePdf, loadPdfJs, openPdf } from "./pdf.js";

const $ = (id) => document.getElementById(id);
const els = {
  app: $("app"), panel: $("viewer"), title: $("viewer-title"), body: $("viewer-body"), wrap: $("page-wrap"),
  canvas: $("page-canvas"), text: $("text-layer"), msg: $("viewer-msg"), page: $("vw-page"), total: $("vw-total"),
  prev: $("vw-prev"), next: $("vw-next"), zin: $("vw-zoom-in"), zout: $("vw-zoom-out"), zoom: $("vw-zoom"),
  close: $("vw-close"), resize: $("viewer-resize"),
};

const state = { docId: null, pdf: null, page: 1, zoom: 1, highlight: "", renderTask: null, textLayer: null, opener: null };
const cache = new Map(); // docId -> PDFDocumentProxy (keep a couple open)

const norm = (s) => s.toLowerCase().normalize("NFKD").replace(/[^\p{L}\p{N}]+/gu, " ").trim();

function showMessage(text) {
  els.msg.textContent = text;
  els.msg.hidden = !text;
  els.wrap.hidden = !!text;
}

function setOpen(open) {
  els.panel.hidden = !open;
  els.app.classList.toggle("viewer-open", open);
}

async function loadDoc(docId) {
  if (cache.has(docId)) return cache.get(docId);
  const rec = await getFile(docId);
  if (!rec) return null;
  const pdf = await openPdf(new Uint8Array(await rec.blob.arrayBuffer()));
  cache.set(docId, pdf);
  if (cache.size > 3) {
    const [oldId, old] = cache.entries().next().value;
    if (oldId !== docId) { closePdf(old); cache.delete(oldId); }
  }
  return pdf;
}

/** Mark text-layer spans that cover the cited chunk (best effort, fuzzy). */
function highlightText(target) {
  const spans = [...els.text.querySelectorAll("span")].filter((s) => !s.classList.contains("markedContent") && s.textContent.trim());
  if (!spans.length || !target) return null;
  let hay = "";
  const owner = [];
  spans.forEach((s, i) => {
    const t = norm(s.textContent);
    if (!t) return;
    for (const ch of t) { hay += ch; owner.push(i); }
    hay += " ";
    owner.push(i);
  });
  const words = norm(target).split(" ").filter(Boolean);
  const marked = new Set();
  const win = Math.min(6, words.length);
  if (!win) return null;
  for (let start = 0; start + win <= words.length; start += 3) {
    const needle = words.slice(start, start + win).join(" ");
    if (needle.length < 12 && words.length > win) continue;
    let pos = hay.indexOf(needle);
    while (pos !== -1) {
      for (let k = pos; k < pos + needle.length; k++) marked.add(owner[k]);
      pos = hay.indexOf(needle, pos + needle.length);
      break; // first occurrence per window is enough
    }
  }
  if (!marked.size) return null;
  const idx = [...marked].sort((a, b) => a - b);
  // Fill small gaps so the highlight reads as one passage.
  for (let j = 1; j < idx.length; j++) {
    if (idx[j] - idx[j - 1] <= 3) for (let k = idx[j - 1]; k <= idx[j]; k++) marked.add(k);
  }
  let first = null;
  [...marked].sort((a, b) => a - b).forEach((i) => {
    spans[i].classList.add("hl");
    first ??= spans[i];
  });
  return first;
}

async function render() {
  const pdf = state.pdf;
  if (!pdf) return;
  const lib = await loadPdfJs();
  state.renderTask?.cancel();
  state.textLayer?.cancel?.();
  const n = Math.min(Math.max(1, state.page), pdf.numPages);
  state.page = n;
  els.page.value = n;
  els.page.max = pdf.numPages;
  els.total.textContent = `/ ${pdf.numPages}`;
  els.prev.disabled = n <= 1;
  els.next.disabled = n >= pdf.numPages;
  const page = await pdf.getPage(n);
  const base = page.getViewport({ scale: 1 });
  const avail = Math.max(240, els.body.clientWidth - 32);
  const scale = (avail / base.width) * state.zoom;
  const viewport = page.getViewport({ scale });
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const c = els.canvas;
  c.width = Math.floor(viewport.width * dpr);
  c.height = Math.floor(viewport.height * dpr);
  c.style.width = `${Math.floor(viewport.width)}px`;
  c.style.height = `${Math.floor(viewport.height)}px`;
  els.wrap.style.width = `${Math.floor(viewport.width)}px`;
  els.wrap.style.height = `${Math.floor(viewport.height)}px`;
  els.wrap.style.setProperty("--scale-factor", String(scale));
  els.wrap.style.setProperty("--total-scale-factor", String(scale));
  els.wrap.style.setProperty("--scale-round-x", "1px");
  els.wrap.style.setProperty("--scale-round-y", "1px");
  els.zoom.textContent = `${Math.round(state.zoom * 100)}%`;
  const ctx = c.getContext("2d", { alpha: false });
  state.renderTask = page.render({ canvasContext: ctx, canvas: c, viewport, transform: [dpr, 0, 0, dpr, 0, 0] });
  try {
    await state.renderTask.promise;
  } catch (e) {
    if (e?.name === "RenderingCancelledException") return;
    throw e;
  }
  els.text.replaceChildren();
  state.textLayer = new lib.TextLayer({ textContentSource: page.streamTextContent(), container: els.text, viewport });
  await state.textLayer.render();
  if (state.highlight) {
    const first = highlightText(state.highlight);
    if (first) {
      const top = first.getBoundingClientRect().top - els.wrap.getBoundingClientRect().top;
      els.body.scrollTo({ top: Math.max(0, top - 80), behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    }
  }
}

export async function openViewer({ docId, docName, page = 1, highlight = "" }) {
  state.opener = document.activeElement;
  setOpen(true);
  els.title.textContent = docName ? `${docName} · p.${page}` : "Document";
  els.title.title = docName || "";
  state.highlight = highlight;
  state.page = page;
  if (state.docId !== docId || !state.pdf) {
    showMessage("Loading…");
    state.zoom = 1;
    try {
      state.pdf = await loadDoc(docId);
    } catch {
      state.pdf = null;
    }
    state.docId = docId;
    if (!state.pdf) {
      showMessage("The original file isn't stored on this device, so it can't be shown. Re-upload it to view pages here.");
      return;
    }
  }
  showMessage("");
  els.body.scrollTop = 0;
  await render();
  els.close.focus({ preventScroll: true });
}

export function closeViewer() {
  setOpen(false);
  state.renderTask?.cancel();
  state.opener?.focus?.({ preventScroll: true });
}

export function forgetDoc(docId) {
  const pdf = cache.get(docId);
  if (pdf) { closePdf(pdf); cache.delete(docId); }
  if (state.docId === docId) { state.docId = null; state.pdf = null; closeViewer(); }
}

export const isViewerOpen = () => !els.panel.hidden;

function go(page) {
  state.page = page;
  els.title.textContent = els.title.title ? `${els.title.title} · p.${page}` : els.title.textContent;
  render();
}

export function initViewer() {
  els.close.addEventListener("click", closeViewer);
  els.prev.addEventListener("click", () => { state.highlight = ""; go(state.page - 1); });
  els.next.addEventListener("click", () => { state.highlight = ""; go(state.page + 1); });
  els.page.addEventListener("change", () => { state.highlight = ""; go(parseInt(els.page.value, 10) || 1); });
  els.zin.addEventListener("click", () => { state.zoom = Math.min(3, +(state.zoom + 0.25).toFixed(2)); render(); });
  els.zout.addEventListener("click", () => { state.zoom = Math.max(0.5, +(state.zoom - 0.25).toFixed(2)); render(); });
  els.panel.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeViewer();
    if (e.target === els.page) return;
    if (e.key === "ArrowRight" || e.key === "PageDown") { if (state.pdf && state.page < state.pdf.numPages) { state.highlight = ""; go(state.page + 1); } }
    if (e.key === "ArrowLeft" || e.key === "PageUp") { if (state.page > 1) { state.highlight = ""; go(state.page - 1); } }
  });

  // Resizable panel (desktop): drag or arrow keys on the separator.
  const saved = parseFloat(localStorage.getItem("docpilot.viewerWidth") || "");
  if (saved > 20 && saved < 80) document.documentElement.style.setProperty("--viewer-w", `${saved}vw`);
  let dragging = false;
  const setWidth = (vw) => {
    const clamped = Math.min(70, Math.max(28, vw));
    document.documentElement.style.setProperty("--viewer-w", `${clamped}vw`);
    localStorage.setItem("docpilot.viewerWidth", String(clamped));
  };
  els.resize.addEventListener("pointerdown", (e) => { dragging = true; els.resize.setPointerCapture(e.pointerId); });
  els.resize.addEventListener("pointermove", (e) => { if (dragging) setWidth(((window.innerWidth - e.clientX) / window.innerWidth) * 100); });
  els.resize.addEventListener("pointerup", () => { if (dragging) { dragging = false; render(); } });
  els.resize.addEventListener("keydown", (e) => {
    const cur = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--viewer-w")) || 45;
    if (e.key === "ArrowLeft") { setWidth(cur + 2); render(); }
    if (e.key === "ArrowRight") { setWidth(cur - 2); render(); }
  });
  let t;
  window.addEventListener("resize", () => { clearTimeout(t); t = setTimeout(() => isViewerOpen() && render(), 150); });
}
