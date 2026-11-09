"""Vector + full-text store with hybrid (RRF) retrieval.

* `MemoryStore`  - local dev / tests. Cosine + BM25 in Python, merged with RRF.
* `SupabaseStore` - production. Postgres + pgvector via PostgREST, hybrid in SQL.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx

from .httpx_retry import UpstreamError
from .pdf import Chunk

RRF_K = 60


@dataclass
class DocRecord:
    id: str
    session_id: str
    name: str
    size_bytes: int = 0
    sha256: str = ""
    pages: int = 0
    chunks: int = 0
    ocr_pages: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def public(self) -> dict:
        d = asdict(self)
        d.pop("session_id", None)
        return d


@dataclass
class Retrieved:
    id: str
    doc_id: str
    doc_name: str
    page: int
    chunk_index: int
    content: str
    similarity: float = 0.0
    rank: float = 0.0


class Store(Protocol):
    kind: str

    async def create_doc(self, doc: DocRecord) -> DocRecord: ...
    async def get_doc(self, session_id: str, doc_id: str) -> DocRecord | None: ...
    async def find_duplicate(
        self, session_id: str, name: str, size: int, sha256: str
    ) -> DocRecord | None: ...
    async def list_docs(self, session_id: str) -> list[DocRecord]: ...
    async def delete_doc(self, session_id: str, doc_id: str) -> bool: ...
    async def clear_session(self, session_id: str) -> int: ...
    async def add_chunks(
        self, session_id: str, doc_id: str, chunks: list[Chunk], embeddings: list[list[float]], ocr_pages: int
    ) -> DocRecord: ...
    async def search(
        self,
        session_id: str,
        query_text: str,
        query_embedding: list[float],
        doc_ids: list[str] | None,
        k: int,
        candidate_k: int,
    ) -> list[Retrieved]: ...
    async def first_chunks(self, session_id: str, doc_id: str, n: int) -> list[Retrieved]: ...
    async def record_event(self, ip_hash: str, session_id: str | None, kind: str) -> None: ...
    async def count_events(self, ip_hash: str, kind: str, since_seconds: int) -> int: ...
    async def cleanup(self, days: int) -> dict: ...


# ------------------------------------------------------------------ helpers
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "does",
        "do",
        "did",
        "can",
        "about",
        "into",
        "than",
        "then",
        "there",
        "these",
        "those",
    ]
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1]


def rrf_merge(rankings: list[list[str]], k: int = RRF_K) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion: score(d) = sum over lists of 1 / (k + rank), rank starting at 1."""
    scores: dict[str, float] = {}
    first_seen: dict[str, int] = {}
    order = 0
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
            if item not in first_seen:
                first_seen[item] = order
                order += 1
    return sorted(scores.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


# ------------------------------------------------------------------ memory
@dataclass
class _MemChunk:
    id: str
    session_id: str
    doc_id: str
    page: int
    chunk_index: int
    content: str
    embedding: list[float]
    tokens: Counter
    created: float


class MemoryStore:
    """Process-local store. Fine for local dev; data is lost on restart."""

    kind = "memory"

    def __init__(self) -> None:
        self.docs: dict[str, DocRecord] = {}
        self.chunks: list[_MemChunk] = []
        self.events: list[tuple[str, str | None, str, float]] = []
        self._lock = asyncio.Lock()

    async def create_doc(self, doc: DocRecord) -> DocRecord:
        self.docs[doc.id] = doc
        return doc

    async def get_doc(self, session_id: str, doc_id: str) -> DocRecord | None:
        d = self.docs.get(doc_id)
        return d if d and d.session_id == session_id else None

    async def find_duplicate(self, session_id: str, name: str, size: int, sha256: str) -> DocRecord | None:
        for d in self.docs.values():
            if d.session_id == session_id and d.name == name and d.size_bytes == size and d.sha256 == sha256:
                return d
        return None

    async def list_docs(self, session_id: str) -> list[DocRecord]:
        return sorted(
            (d for d in self.docs.values() if d.session_id == session_id), key=lambda d: d.created_at
        )

    async def delete_doc(self, session_id: str, doc_id: str) -> bool:
        async with self._lock:
            d = self.docs.get(doc_id)
            if not d or d.session_id != session_id:
                return False
            del self.docs[doc_id]
            self.chunks = [c for c in self.chunks if c.doc_id != doc_id]
            return True

    async def clear_session(self, session_id: str) -> int:
        async with self._lock:
            ids = [d.id for d in self.docs.values() if d.session_id == session_id]
            for i in ids:
                del self.docs[i]
            self.chunks = [c for c in self.chunks if c.session_id != session_id]
            return len(ids)

    async def add_chunks(
        self, session_id: str, doc_id: str, chunks: list[Chunk], embeddings: list[list[float]], ocr_pages: int
    ) -> DocRecord:
        async with self._lock:
            doc = self.docs.get(doc_id)
            if not doc or doc.session_id != session_id:
                raise KeyError(doc_id)
            now = time.time()
            for ch, emb in zip(chunks, embeddings, strict=True):
                self.chunks.append(
                    _MemChunk(
                        id=uuid.uuid4().hex,
                        session_id=session_id,
                        doc_id=doc_id,
                        page=ch.page,
                        chunk_index=ch.chunk_index,
                        content=ch.content,
                        embedding=emb,
                        tokens=Counter(tokenize(ch.content)),
                        created=now,
                    )
                )
            doc.chunks += len(chunks)
            doc.ocr_pages += ocr_pages
            return doc

    def _scope(self, session_id: str, doc_ids: list[str] | None) -> list[_MemChunk]:
        allowed = set(doc_ids) if doc_ids else None
        return [
            c for c in self.chunks if c.session_id == session_id and (allowed is None or c.doc_id in allowed)
        ]

    @staticmethod
    def _bm25(
        scope: list[_MemChunk], query: str, k1: float = 1.5, b: float = 0.75
    ) -> list[tuple[_MemChunk, float]]:
        q = tokenize(query)
        if not q or not scope:
            return []
        n = len(scope)
        avgdl = sum(sum(c.tokens.values()) for c in scope) / n or 1.0
        df = {t: sum(1 for c in scope if t in c.tokens) for t in set(q)}
        scored = []
        for c in scope:
            dl = sum(c.tokens.values()) or 1
            s = 0.0
            for t in q:
                tf = c.tokens.get(t, 0)
                if not tf:
                    continue
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avgdl))
            if s > 0:
                scored.append((c, s))
        scored.sort(key=lambda x: -x[1])
        return scored

    async def search(
        self,
        session_id: str,
        query_text: str,
        query_embedding: list[float],
        doc_ids: list[str] | None,
        k: int,
        candidate_k: int,
    ) -> list[Retrieved]:
        scope = self._scope(session_id, doc_ids)
        if not scope:
            return []
        sims = {c.id: _dot(c.embedding, query_embedding) for c in scope}
        vec_rank = [c.id for c in sorted(scope, key=lambda c: -sims[c.id])[:candidate_k]]
        fts_rank = [c.id for c, _ in self._bm25(scope, query_text)[:candidate_k]]
        by_id = {c.id: c for c in scope}
        out = []
        for cid, score in rrf_merge([vec_rank, fts_rank])[:k]:
            c = by_id[cid]
            out.append(
                Retrieved(
                    id=c.id,
                    doc_id=c.doc_id,
                    doc_name=self.docs[c.doc_id].name,
                    page=c.page,
                    chunk_index=c.chunk_index,
                    content=c.content,
                    similarity=sims[cid],
                    rank=score,
                )
            )
        return out

    async def first_chunks(self, session_id: str, doc_id: str, n: int) -> list[Retrieved]:
        doc = await self.get_doc(session_id, doc_id)
        if not doc:
            return []
        rows = sorted((c for c in self.chunks if c.doc_id == doc_id), key=lambda c: c.chunk_index)[:n]
        return [Retrieved(c.id, c.doc_id, doc.name, c.page, c.chunk_index, c.content) for c in rows]

    async def record_event(self, ip_hash: str, session_id: str | None, kind: str) -> None:
        self.events.append((ip_hash, session_id, kind, time.time()))

    async def count_events(self, ip_hash: str, kind: str, since_seconds: int) -> int:
        cutoff = time.time() - since_seconds
        return sum(1 for ip, _, kd, ts in self.events if ip == ip_hash and kd == kind and ts >= cutoff)

    async def cleanup(self, days: int) -> dict:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        old = [d.id for d in self.docs.values() if datetime.fromisoformat(d.created_at) < cutoff]
        for i in old:
            del self.docs[i]
        self.chunks = [c for c in self.chunks if c.doc_id in self.docs]
        ev_cut = time.time() - max(days, 2) * 86400
        before = len(self.events)
        self.events = [e for e in self.events if e[3] >= ev_cut]
        return {"documents": len(old), "usage_events": before - len(self.events)}


# ------------------------------------------------------------------ supabase
def _vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


class SupabaseStore:
    """Talks to Supabase's PostgREST API with the service-role key (server only)."""

    kind = "supabase"
    INSERT_BATCH = 200

    def __init__(self, url: str, service_key: str, timeout: float = 30):
        self.base = f"{url.rstrip('/')}/rest/v1"
        # New secret keys (sb_secret_...) go on the `apikey` header only; legacy service_role
        # JWTs (eyJ...) are also sent as a Bearer token.
        self.headers = {"apikey": service_key, "Content-Type": "application/json"}
        if service_key.startswith("eyJ"):
            self.headers["Authorization"] = f"Bearer {service_key}"
        self.timeout = timeout

    async def _req(self, method: str, path: str, *, params=None, json=None, prefer: str | None = None):
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(3):
                try:
                    resp = await client.request(
                        method, f"{self.base}/{path}", params=params, json=json, headers=headers
                    )
                except httpx.TransportError as exc:
                    if attempt == 2:
                        raise UpstreamError("Database is unreachable.") from exc
                    await asyncio.sleep(0.5 * 2**attempt)
                    continue
                if resp.status_code >= 500 and attempt < 2:
                    await asyncio.sleep(0.5 * 2**attempt)
                    continue
                if resp.status_code >= 400:
                    msg = ""
                    with contextlib.suppress(Exception):
                        msg = str(resp.json().get("message", ""))[:200]
                    raise UpstreamError(f"Database error {resp.status_code}: {msg}", resp.status_code)
                if not resp.content:
                    return None
                return resp.json()
        return None

    @staticmethod
    def _doc(row: dict) -> DocRecord:
        return DocRecord(
            id=str(row["id"]),
            session_id=row["session_id"],
            name=row["name"],
            size_bytes=int(row.get("size_bytes") or 0),
            sha256=row.get("sha256") or "",
            pages=int(row.get("pages") or 0),
            chunks=int(row.get("chunks") or 0),
            ocr_pages=int(row.get("ocr_pages") or 0),
            created_at=str(row.get("created_at") or ""),
        )

    async def create_doc(self, doc: DocRecord) -> DocRecord:
        body = {k: v for k, v in asdict(doc).items() if k != "created_at"}
        rows = await self._req("POST", "documents", json=body, prefer="return=representation")
        return self._doc(rows[0])

    async def get_doc(self, session_id: str, doc_id: str) -> DocRecord | None:
        try:
            uuid.UUID(doc_id)
        except ValueError:
            return None
        rows = await self._req(
            "GET", "documents", params={"id": f"eq.{doc_id}", "session_id": f"eq.{session_id}", "select": "*"}
        )
        return self._doc(rows[0]) if rows else None

    async def find_duplicate(self, session_id: str, name: str, size: int, sha256: str) -> DocRecord | None:
        rows = await self._req(
            "GET",
            "documents",
            params={
                "session_id": f"eq.{session_id}",
                "name": f"eq.{name}",
                "size_bytes": f"eq.{size}",
                "sha256": f"eq.{sha256}",
                "select": "*",
                "limit": "1",
            },
        )
        return self._doc(rows[0]) if rows else None

    async def list_docs(self, session_id: str) -> list[DocRecord]:
        rows = await self._req("POST", "rpc/list_session_docs", json={"p_session_id": session_id})
        return [self._doc(r) for r in rows or []]

    async def delete_doc(self, session_id: str, doc_id: str) -> bool:
        try:
            uuid.UUID(doc_id)
        except ValueError:
            return False
        rows = await self._req(
            "DELETE",
            "documents",
            params={"id": f"eq.{doc_id}", "session_id": f"eq.{session_id}"},
            prefer="return=representation",
        )
        return bool(rows)

    async def clear_session(self, session_id: str) -> int:
        rows = await self._req(
            "DELETE", "documents", params={"session_id": f"eq.{session_id}"}, prefer="return=representation"
        )
        return len(rows or [])

    async def add_chunks(
        self, session_id: str, doc_id: str, chunks: list[Chunk], embeddings: list[list[float]], ocr_pages: int
    ) -> DocRecord:
        rows = [
            {
                "session_id": session_id,
                "doc_id": doc_id,
                "page": c.page,
                "chunk_index": c.chunk_index,
                "content": c.content,
                "embedding": _vec_literal(e),
            }
            for c, e in zip(chunks, embeddings, strict=True)
        ]
        for i in range(0, len(rows), self.INSERT_BATCH):
            await self._req(
                "POST", "doc_chunks", json=rows[i : i + self.INSERT_BATCH], prefer="return=minimal"
            )
        updated = await self._req(
            "POST",
            "rpc/bump_doc_counts",
            json={
                "p_doc_id": doc_id,
                "p_session_id": session_id,
                "p_chunks": len(rows),
                "p_ocr_pages": ocr_pages,
            },
        )
        if isinstance(updated, list):
            updated = updated[0] if updated else None
        if not updated or not updated.get("id"):
            raise KeyError(doc_id)
        return self._doc(updated)

    async def search(
        self,
        session_id: str,
        query_text: str,
        query_embedding: list[float],
        doc_ids: list[str] | None,
        k: int,
        candidate_k: int,
    ) -> list[Retrieved]:
        rows = await self._req(
            "POST",
            "rpc/match_chunks_hybrid",
            json={
                "query_embedding": _vec_literal(query_embedding),
                "query_text": query_text[:1000],
                "p_session_id": session_id,
                "p_doc_ids": doc_ids or None,
                "match_count": k,
                "candidate_count": candidate_k,
                "rrf_k": RRF_K,
            },
        )
        return [
            Retrieved(
                id=str(r["id"]),
                doc_id=str(r["doc_id"]),
                doc_name=r["doc_name"],
                page=int(r["page"]),
                chunk_index=int(r["chunk_index"]),
                content=r["content"],
                similarity=float(r.get("similarity") or 0),
                rank=float(r.get("rank") or 0),
            )
            for r in rows or []
        ]

    async def first_chunks(self, session_id: str, doc_id: str, n: int) -> list[Retrieved]:
        doc = await self.get_doc(session_id, doc_id)
        if not doc:
            return []
        rows = await self._req(
            "GET",
            "doc_chunks",
            params={
                "doc_id": f"eq.{doc_id}",
                "session_id": f"eq.{session_id}",
                "select": "id,doc_id,page,chunk_index,content",
                "order": "chunk_index.asc",
                "limit": str(n),
            },
        )
        return [
            Retrieved(
                str(r["id"]), str(r["doc_id"]), doc.name, int(r["page"]), int(r["chunk_index"]), r["content"]
            )
            for r in rows or []
        ]

    async def record_event(self, ip_hash: str, session_id: str | None, kind: str) -> None:
        await self._req(
            "POST",
            "usage_events",
            json={"ip_hash": ip_hash, "session_id": session_id, "kind": kind},
            prefer="return=minimal",
        )

    async def count_events(self, ip_hash: str, kind: str, since_seconds: int) -> int:
        since = (datetime.now(UTC) - timedelta(seconds=since_seconds)).isoformat()
        n = await self._req(
            "POST", "rpc/count_usage", json={"p_ip_hash": ip_hash, "p_kind": kind, "p_since": since}
        )
        return int(n or 0)

    async def cleanup(self, days: int) -> dict:
        return await self._req("POST", "rpc/cleanup_old_data", json={"p_days": days}) or {}


def make_store(settings) -> Store:
    if settings.supabase_enabled:
        return SupabaseStore(settings.supabase_url, settings.supabase_service_key)
    return MemoryStore()
