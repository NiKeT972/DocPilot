// Browser-side ingestion: parse the PDF with pdf.js, OCR scanned pages, send text in batches.
import { api, ApiError } from "./api.js";
import { closePdf, openPdf, renderPageJpeg, textFromContent } from "./pdf.js";

const enc = new TextEncoder();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function sha256Hex(buffer) {
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

const cleanLen = (t) => t.replace(/\s+/g, " ").trim().length;

/**
 * Ingest one file. `cb` receives progress updates:
 *   {phase:"reading", done, total} | {phase:"ocr", page} | {phase:"indexing", pct} | {phase:"retrying", attempt}
 * Returns the final server response for the last batch.
 */
export async function ingestFile({ file, buffer, sessionId, limits, ocrEnabled, cb, signal }) {
  const sha = await sha256Hex(buffer);
  // pdf.js transfers the buffer to its worker; give it a copy so we keep the original for IndexedDB.
  const pdf = await openPdf(new Uint8Array(buffer.slice(0)));
  try {
    const total = pdf.numPages;
    if (total > limits.max_pages) {
      throw new ApiError(`This PDF has ${total} pages; the limit is ${limits.max_pages}.`, 413, "too_many_pages");
    }
    // 1) Read text (and OCR scanned pages, capped per file).
    const pages = [];
    let ocrUsed = 0;
    let ocrSkipped = 0;
    for (let n = 1; n <= total; n++) {
      if (signal?.aborted) throw new DOMException("aborted", "AbortError");
      const page = await pdf.getPage(n);
      let text = "";
      try {
        text = textFromContent(await page.getTextContent());
      } catch { text = ""; }
      let ocr = false;
      if (cleanLen(text) < 20) {
        if (ocrEnabled && ocrUsed < limits.max_ocr_pages_per_file) {
          cb({ phase: "ocr", page: n, done: n, total });
          try {
            const image = await renderPageJpeg(page);
            const res = await api("/api/ocr", { method: "POST", body: { session_id: sessionId, image_base64: image }, signal });
            text = res.text || "";
            ocr = true;
            ocrUsed++;
          } catch (e) {
            if (e.name === "AbortError") throw e;
            if (e.status === 429 || e.status === 503) {
              ocrSkipped++;
              ocrEnabled = false; // stop trying for the rest of this file
              cb({ phase: "ocr-off", message: e.message });
            } else ocrSkipped++;
          }
        } else if (cleanLen(text) === 0) ocrSkipped++;
      }
      page.cleanup();
      pages.push({ page: n, text, ocr });
      cb({ phase: "reading", done: n, total });
    }

    // 2) Batch: <=max_batch_pages pages and <=max_batch_bytes per request.
    const maxPages = limits.max_batch_pages;
    const maxBytes = limits.max_batch_bytes - 20_000;
    const batches = [];
    let cur = [];
    let bytes = 0;
    for (const p of pages) {
      const size = enc.encode(p.text).length + 40;
      if (cur.length && (cur.length >= maxPages || bytes + size > maxBytes)) {
        batches.push(cur);
        cur = [];
        bytes = 0;
      }
      cur.push(p);
      bytes += size;
    }
    if (cur.length || !batches.length) batches.push(cur);

    // 3) Send sequentially with retries.
    let docId = null;
    let last = null;
    let sentPages = 0;
    cb({ phase: "indexing", pct: 0 });
    for (let b = 0; b < batches.length; b++) {
      const body = {
        session_id: sessionId,
        doc_id: docId,
        doc_name: file.name,
        sha256: sha,
        size_bytes: file.size,
        total_pages: total,
        pages: batches[b],
        first_batch: b === 0,
        last_batch: b === batches.length - 1,
      };
      for (let attempt = 0; ; attempt++) {
        try {
          last = await api("/api/ingest", { method: "POST", body, signal });
          break;
        } catch (e) {
          const retryable = e.name !== "AbortError" && (e.status === 0 || e.status >= 500) && e.code !== "network-offline";
          if (!retryable || attempt >= 3) throw e;
          cb({ phase: "retrying", attempt: attempt + 1 });
          await sleep(1000 * 2 ** attempt + Math.random() * 300);
        }
      }
      docId = last.doc_id;
      if (b === 0) cb({ phase: "created", docId, replaced: last.replaced, sha });
      sentPages += batches[b].length;
      cb({ phase: "indexing", pct: Math.round((sentPages / total) * 100) });
    }
    return { ...last, sha, ocrUsed, ocrSkipped };
  } finally {
    closePdf(pdf);
  }
}
