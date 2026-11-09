"""Tavily web search. Failures never break a chat; the graph records them in the trace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

import httpx

TAVILY_URL = "https://api.tavily.com/search"
MAX_CONTENT_CHARS = 1500


@dataclass
class WebResult:
    title: str
    url: str
    content: str

    @property
    def domain(self) -> str:
        return urlparse(self.url).netloc.removeprefix("www.")


class WebSearch(Protocol):
    async def search(self, query: str, max_results: int = 5) -> list[WebResult]: ...


class TavilySearch:
    def __init__(self, api_key: str, timeout: float = 12):
        self.api_key = api_key
        self.timeout = timeout

    async def search(self, query: str, max_results: int = 5) -> list[WebResult]:
        body = {
            "query": query[:400],
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                TAVILY_URL, json=body, headers={"Authorization": f"Bearer {self.api_key}"}
            )
            resp.raise_for_status()
            data = resp.json()
        out = []
        for r in data.get("results", [])[:max_results]:
            url = str(r.get("url") or "")
            if not url.startswith(("http://", "https://")):
                continue
            out.append(
                WebResult(
                    title=str(r.get("title") or url)[:200],
                    url=url,
                    content=" ".join(str(r.get("content") or "").split())[:MAX_CONTENT_CHARS],
                )
            )
        return out
