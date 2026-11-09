// pdf.js loader (bundled locally in /vendor/pdfjs, no CDN).
let libPromise = null;

export function loadPdfJs() {
  if (!libPromise) {
    libPromise = import("/vendor/pdfjs/pdf.min.mjs").then((lib) => {
      lib.GlobalWorkerOptions.workerSrc = "/vendor/pdfjs/pdf.worker.min.mjs";
      return lib;
    });
  }
  return libPromise;
}

export async function openPdf(data) {
  const lib = await loadPdfJs();
  const task = lib.getDocument({
    data,
    cMapUrl: "/vendor/pdfjs/cmaps/",
    cMapPacked: true,
    standardFontDataUrl: "/vendor/pdfjs/standard_fonts/",
    wasmUrl: "/vendor/pdfjs/wasm/",
    iccUrl: "/vendor/pdfjs/iccs/",
    isEvalSupported: false,
    enableXfa: false,
  });
  const doc = await task.promise;
  tasks.set(doc, task);
  return doc;
}

const tasks = new WeakMap();

/** Release a document and its worker resources. */
export function closePdf(doc) {
  if (!doc) return;
  const task = tasks.get(doc);
  if (task) task.destroy();
  else doc.destroy?.();
}

/** Join pdf.js text items into page text, keeping line breaks. */
export function textFromContent(content) {
  let out = "";
  for (const item of content.items) {
    if (typeof item.str !== "string") continue;
    out += item.str;
    if (item.hasEOL) out += "\n";
    else if (item.str && !/\s$/.test(item.str)) out += " ";
  }
  return out;
}

/** Render a page to a JPEG data URL at ~dpi (capped) for OCR. */
export async function renderPageJpeg(page, dpi = 150, maxSide = 2200, quality = 0.82) {
  let scale = dpi / 72;
  const base = page.getViewport({ scale: 1 });
  scale = Math.min(scale, maxSide / Math.max(base.width, base.height));
  const viewport = page.getViewport({ scale });
  const canvas = document.createElement("canvas");
  canvas.width = Math.floor(viewport.width);
  canvas.height = Math.floor(viewport.height);
  const ctx = canvas.getContext("2d", { alpha: false });
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  await page.render({ canvasContext: ctx, canvas, viewport }).promise;
  let url = canvas.toDataURL("image/jpeg", quality);
  if (url.length > 2_600_000) url = canvas.toDataURL("image/jpeg", 0.6);
  canvas.width = canvas.height = 0;
  return url;
}
