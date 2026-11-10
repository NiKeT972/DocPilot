"""Groq chat completions (OpenAI-compatible REST), with streaming.

Model names come from env (ANSWER_MODEL / FAST_MODEL). If Groq reports that a
model doesn't exist we raise a clear, user-safe error instead of a stack trace.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Protocol

import httpx

from .httpx_retry import RETRY_STATUS, UpstreamError, _retry_after, post_json

GROQ_BASE = "https://api.groq.com/openai/v1"

Message = dict[str, str]


class LLM(Protocol):
    model: str

    async def complete(
        self, messages: list[Message], *, max_tokens: int = 1024, json_mode: bool = False
    ) -> str: ...

    def stream(self, messages: list[Message], *, max_tokens: int = 2048) -> AsyncIterator[str]: ...


class ModelNotFoundError(UpstreamError):
    pass


def _model_error(model: str, status: int | None, message: str) -> UpstreamError:
    low = message.lower()
    if (
        status == 404
        or "model_not_found" in low
        or ("model" in low and "does not exist" in low)
        or "decommissioned" in low
    ):
        return ModelNotFoundError(
            f"Groq model '{model}' is not available (it may have been renamed or retired). "
            "Set ANSWER_MODEL / FAST_MODEL to a model listed at GET https://api.groq.com/openai/v1/models.",
            status,
        )
    if status == 401:
        return UpstreamError("Groq rejected the API key (401). Check GROQ_API_KEY.", status)
    if status == 429:
        return UpstreamError(
            "The language model is rate-limited right now. Please retry in a minute.", status
        )
    return UpstreamError(message, status)


class GroqLLM:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        temperature: float = 0.2,
        reasoning_effort: str = "low",
        timeout: float = 90,
    ):
        if not api_key:
            raise UpstreamError("GROQ_API_KEY is not configured.")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self.stream_retries = 3

    def _body(self, messages: list[Message], max_tokens: int, stream: bool, json_mode: bool = False) -> dict:
        body: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_completion_tokens": max_tokens,
            "stream": stream,
        }
        if self.model.startswith("openai/gpt-oss"):
            # Keep reasoning out of the answer text and keep it fast.
            body["reasoning_effort"] = self.reasoning_effort
            body["include_reasoning"] = False
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def complete(
        self, messages: list[Message], *, max_tokens: int = 1024, json_mode: bool = False
    ) -> str:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                data = await post_json(
                    client,
                    f"{GROQ_BASE}/chat/completions",
                    json=self._body(messages, max_tokens, False, json_mode),
                    headers=self._headers,
                    retries=2,
                    service="Groq",
                )
            except UpstreamError as exc:
                if json_mode and exc.status == 400:
                    # JSON mode can fail validation on odd outputs; retry once without it and parse defensively.
                    return await self.complete(messages, max_tokens=max_tokens, json_mode=False)
                raise _model_error(self.model, exc.status, str(exc)) from exc
        try:
            return data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError):
            return ""

    async def stream(self, messages: list[Message], *, max_tokens: int = 2048) -> AsyncIterator[str]:
        """Stream answer tokens. Rate limits and 5xx before the first token are retried."""
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, read=60)) as client:
            for attempt in range(self.stream_retries + 1):
                try:
                    async with client.stream(
                        "POST",
                        f"{GROQ_BASE}/chat/completions",
                        json=self._body(messages, max_tokens, True),
                        headers=self._headers,
                    ) as resp:
                        if resp.status_code >= 400:
                            raw = (await resp.aread()).decode("utf-8", "replace")
                            if resp.status_code in RETRY_STATUS and attempt < self.stream_retries:
                                delay = _retry_after(resp) or 2.0 * 2**attempt
                                await asyncio.sleep(min(30.0, max(1.0, delay)))
                                continue
                            msg = raw
                            with contextlib.suppress(Exception):
                                msg = json.loads(raw).get("error", {}).get("message", raw)
                            raise _model_error(
                                self.model, resp.status_code, f"Groq error {resp.status_code}: {msg[:300]}"
                            )
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if payload == "[DONE]":
                                break
                            try:
                                delta = json.loads(payload)["choices"][0].get("delta", {}).get("content")
                            except (ValueError, KeyError, IndexError):
                                continue
                            if delta:
                                yield delta
                        return
                except httpx.TransportError as exc:
                    raise UpstreamError("Lost connection to the language model.") from exc
