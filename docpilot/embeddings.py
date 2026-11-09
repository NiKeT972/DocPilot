"""Gemini embeddings over REST (batchEmbedContents), L2-normalised.

gemini-embedding-001 only returns normalised vectors at 3072 dims; for smaller
`outputDimensionality` (we use 768) the docs say to normalise manually.
"""

from __future__ import annotations

import math
from typing import Protocol

import httpx

from .httpx_retry import UpstreamError, post_json

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
BATCH_SIZE = 100
# Keep each call well under per-minute token budgets (~4 chars/token).
MAX_BATCH_CHARS = 60_000


class Embedder(Protocol):
    dim: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


def l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def _batches(texts: list[str]) -> list[list[str]]:
    """Split into calls of <= BATCH_SIZE texts and <= MAX_BATCH_CHARS characters."""
    groups: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for t in texts:
        n = min(len(t), 8000)
        if cur and (len(cur) >= BATCH_SIZE or size + n > MAX_BATCH_CHARS):
            groups.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += n
    if cur:
        groups.append(cur)
    return groups


class GeminiEmbedder:
    def __init__(
        self, api_key: str, model: str = "gemini-embedding-001", dim: int = 768, timeout: float = 60
    ):
        if not api_key:
            raise UpstreamError("GEMINI_API_KEY is not configured.")
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self.timeout = timeout

    async def _batch(self, client: httpx.AsyncClient, texts: list[str], task: str) -> list[list[float]]:
        body = {
            "requests": [
                {
                    "model": f"models/{self.model}",
                    "content": {"parts": [{"text": t[:8000]}]},
                    "taskType": task,
                    "outputDimensionality": self.dim,
                }
                for t in texts
            ]
        }
        try:
            data = await post_json(
                client,
                f"{GEMINI_BASE}/models/{self.model}:batchEmbedContents",
                json=body,
                headers={"x-goog-api-key": self.api_key},
                retries=4,  # 4 x <=65 s stays inside Vercel's 300 s limit
                max_delay=65,  # free-tier per-minute quotas ask us to wait up to a minute
                min_429_delay=10,  # 10, 20, 40, 60 s: ~130 s worst case
                service="Gemini embeddings",
            )
        except UpstreamError as exc:
            if exc.status == 429:
                raise UpstreamError(
                    "The embedding quota for the Gemini API key is used up for now. "
                    "Please try again later (free-tier keys have small per-minute and per-day limits).",
                    429,
                ) from exc
            raise
        embs = data.get("embeddings") or []
        if len(embs) != len(texts):
            raise UpstreamError("Gemini embeddings returned an unexpected number of vectors.")
        return [l2_normalize(list(e.get("values") or [])) for e in embs]

    async def _embed(self, texts: list[str], task: str) -> list[list[float]]:
        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for group in _batches(texts):
                out.extend(await self._batch(client, group, task))
        return out

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await self._embed(texts, "RETRIEVAL_DOCUMENT")

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text], "RETRIEVAL_QUERY"))[0]
