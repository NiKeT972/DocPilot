// IndexedDB storage for the original PDF bytes, so the viewer survives reloads.
// Keyed by server doc_id; scoped by session id.

const DB = "docpilot";
const STORE = "files";
let dbp = null;

function open() {
  if (dbp) return dbp;
  dbp = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB, 1);
    req.onupgradeneeded = () => {
      const s = req.result.createObjectStore(STORE, { keyPath: "docId" });
      s.createIndex("session", "sessionId");
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return dbp;
}

async function tx(mode, fn) {
  const db = await open();
  return new Promise((resolve, reject) => {
    const t = db.transaction(STORE, mode);
    const result = fn(t.objectStore(STORE));
    t.oncomplete = () => resolve(result?.result ?? result);
    t.onerror = () => reject(t.error);
    t.onabort = () => reject(t.error);
  });
}

export async function saveFile(sessionId, docId, name, blob) {
  try {
    await tx("readwrite", (s) => s.put({ docId, sessionId, name, blob, savedAt: Date.now() }));
  } catch (e) {
    console.warn("Could not keep a local copy of the PDF", e?.name);
  }
}

export async function getFile(docId) {
  try {
    const db = await open();
    return await new Promise((resolve, reject) => {
      const req = db.transaction(STORE).objectStore(STORE).get(docId);
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => reject(req.error);
    });
  } catch {
    return null;
  }
}

export async function deleteFile(docId) {
  try { await tx("readwrite", (s) => s.delete(docId)); } catch { /* ignore */ }
}

export async function clearSession(sessionId) {
  try {
    const db = await open();
    await new Promise((resolve, reject) => {
      const t = db.transaction(STORE, "readwrite");
      const idx = t.objectStore(STORE).index("session");
      const req = idx.openCursor(IDBKeyRange.only(sessionId));
      req.onsuccess = () => {
        const cur = req.result;
        if (cur) { cur.delete(); cur.continue(); }
      };
      t.oncomplete = resolve;
      t.onerror = () => reject(t.error);
    });
  } catch { /* ignore */ }
}
