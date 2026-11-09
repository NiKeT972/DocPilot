"""Wires settings to concrete clients. Tests swap these for fakes via `set_services`."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings, get_settings
from .embeddings import Embedder, GeminiEmbedder
from .graph import Deps
from .llm import LLM, GroqLLM
from .ocr import GeminiOCR
from .ratelimit import RateLimiter
from .store import Store, make_store
from .web import TavilySearch, WebSearch


@dataclass
class Services:
    settings: Settings
    store: Store
    embedder: Embedder | None
    fast_llm: LLM | None
    answer_llm: LLM | None
    web: WebSearch | None
    limiter: RateLimiter
    ocr: GeminiOCR | None = None

    def deps(self) -> Deps:
        return Deps(
            settings=self.settings,
            store=self.store,
            embedder=self.embedder,
            fast_llm=self.fast_llm,
            answer_llm=self.answer_llm,
            web=self.web,
        )


def build_services(settings: Settings | None = None) -> Services:
    s = settings or get_settings()
    s.validate()
    store = make_store(s)
    return Services(
        settings=s,
        store=store,
        embedder=GeminiEmbedder(s.gemini_api_key, s.embed_model, s.embed_dim) if s.gemini_api_key else None,
        fast_llm=GroqLLM(s.groq_api_key, s.fast_model, temperature=0.0) if s.groq_api_key else None,
        answer_llm=GroqLLM(s.groq_api_key, s.answer_model, temperature=0.2) if s.groq_api_key else None,
        web=TavilySearch(s.tavily_api_key) if s.tavily_api_key else None,
        limiter=RateLimiter(store, s.ip_hash_salt),
        ocr=GeminiOCR(s.gemini_api_key, s.ocr_model) if s.gemini_api_key else None,
    )


_services: Services | None = None


def get_services() -> Services:
    global _services
    if _services is None:
        _services = build_services()
    return _services


def set_services(services: Services | None) -> None:
    global _services
    _services = services
