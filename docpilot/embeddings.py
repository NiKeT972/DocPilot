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


class Embedder(Protocol):
    dim: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


def l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


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
        data = await post_json(
            client,
            f"{GEMINI_BASE}/models/{self.model}:batchEmbedContents",
            json=body,
            headers={"x-goog-api-key": self.api_key},
            service="Gemini embeddings",
        )
        embs = data.get("embeddings") or []
        if len(embs) != len(texts):
            raise UpstreamError("Gemini embeddings returned an unexpected number of vectors.")
        return [l2_normalize(list(e.get("values") or [])) for e in embs]

    async def _embed(self, texts: list[str], task: str) -> list[list[float]]:
        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for i in range(0, len(texts), BATCH_SIZE):
                out.extend(await self._batch(client, texts[i : i + BATCH_SIZE], task))
        return out

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await self._embed(texts, "RETRIEVAL_DOCUMENT")

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text], "RETRIEVAL_QUERY"))[0]
