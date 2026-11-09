"""Offline fakes for every external dependency, so the test suite needs no API keys."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import AsyncIterator, Callable

from docpilot.config import Settings
from docpilot.pdf import PageText, chunk_pages
from docpilot.ratelimit import RateLimiter
from docpilot.services import Services
from docpilot.store import DocRecord, MemoryStore
from docpilot.web import WebResult

Messages = list[dict[str, str]]


class FakeEmbedder:
    """Hashed bag-of-words embedding: texts sharing words get similar vectors."""

    dim = 64

    def __init__(self) -> None:
        self.calls = 0

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for w in re.findall(r"[a-z0-9]+", text.lower()):
            if len(w) < 3:
                continue
            h = int(hashlib.md5(w.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._vec(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        self.calls += 1
        return self._vec(text)


class FakeFastLLM:
    """Plays the grader / condenser / query rewriter based on the system prompt."""

    model = "fake-fast"

    def __init__(
        self,
        grade: dict | str | Callable[[str], dict | str] | None = None,
        condense: str | None = None,
        rewrite: str = "fake web query",
        suggest: str | None = None,
    ):
        self.grade = grade
        self.condense = condense
        self.rewrite = rewrite
        self.suggest = suggest
        self.calls: list[tuple[str, Messages]] = []

    async def complete(self, messages: Messages, *, max_tokens: int = 1024, json_mode: bool = False) -> str:
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if "grade retrieved PDF excerpts" in system:
            self.calls.append(("grade", messages))
            g = self.grade(user) if callable(self.grade) else self.grade
            if g is None:
                n = len(re.findall(r"<<<PDF_EXCERPT id=", user))
                g = {"relevant": list(range(1, n + 1)), "fully_answers": True, "missing": ""}
            return g if isinstance(g, str) else json.dumps(g)
        if "standalone question" in system:
            self.calls.append(("condense", messages))
            return self.condense or user.split("Latest message:")[-1].split("\n")[0].strip()
        if "web search query" in system:
            self.calls.append(("rewrite", messages))
            return self.rewrite
        if "starter questions" in system:
            self.calls.append(("suggest", messages))
            return self.suggest or json.dumps(
                {"questions": ["What is this about?", "Key findings?", "Who wrote it?"]}
            )
        self.calls.append(("other", messages))
        return ""

    async def stream(self, messages: Messages, *, max_tokens: int = 2048) -> AsyncIterator[str]:
        yield await self.complete(messages, max_tokens=max_tokens)


class FakeAnswerLLM:
    """Streams a scripted answer (string or function of the prompt) word by word."""

    model = "fake-answer"

    def __init__(self, answer: str | Callable[[Messages], str] = "Answer [P1]."):
        self.answer = answer
        self.calls: list[Messages] = []

    async def complete(self, messages: Messages, *, max_tokens: int = 1024, json_mode: bool = False) -> str:
        self.calls.append(messages)
        return self.answer(messages) if callable(self.answer) else self.answer

    async def stream(self, messages: Messages, *, max_tokens: int = 2048) -> AsyncIterator[str]:
        self.calls.append(messages)
        text = self.answer(messages) if callable(self.answer) else self.answer
        for piece in re.findall(r"\S+\s*", text):
            yield piece


class FakeWeb:
    def __init__(self, results: list[WebResult] | None = None, fail: bool = False):
        self.results = (
            results
            if results is not None
            else [
                WebResult("Example news", "https://news.example.com/a", "Fresh web fact about the topic."),
                WebResult("Second source", "https://www.example.org/b", "Another web fact."),
            ]
        )
        self.fail = fail
        self.queries: list[str] = []

    async def search(self, query: str, max_results: int = 5) -> list[WebResult]:
        self.queries.append(query)
        if self.fail:
            raise TimeoutError("tavily timed out")
        return self.results[:max_results]


def settings_for_tests(**overrides) -> Settings:
    base = {
        "groq_api_key": "test",
        "gemini_api_key": "test",
        "tavily_api_key": "test",
        "rate_chat_per_hour": 1000,
        "rate_ingest_per_day": 10_000,
        "rate_ocr_per_day": 1000,
    }
    base.update(overrides)
    return Settings(**base)


def make_services(
    *,
    settings: Settings | None = None,
    fast: FakeFastLLM | None = None,
    answer: FakeAnswerLLM | None = None,
    web: FakeWeb | bool | None = True,
    store: MemoryStore | None = None,
) -> Services:
    s = settings or settings_for_tests()
    st = store or MemoryStore()
    return Services(
        settings=s,
        store=st,
        embedder=FakeEmbedder(),
        fast_llm=fast or FakeFastLLM(),
        answer_llm=answer or FakeAnswerLLM(),
        web=(FakeWeb() if web is True else (web or None)),
        limiter=RateLimiter(st, s.ip_hash_salt),
    )


async def add_doc(services: Services, session_id: str, name: str, pages: list[str]) -> DocRecord:
    """Index a document directly (bypassing the HTTP ingest path)."""
    doc = await services.store.create_doc(
        DocRecord(
            id=hashlib.md5(f"{session_id}{name}".encode()).hexdigest()[:8] + "-0000-4000-8000-000000000000",
            session_id=session_id,
            name=name,
            pages=len(pages),
        )
    )
    chunks = chunk_pages([PageText(i + 1, t) for i, t in enumerate(pages)], doc.id, name, 400, 50)
    embs = await services.embedder.embed_documents([c.content for c in chunks])
    return await services.store.add_chunks(session_id, doc.id, chunks, embs, 0)
