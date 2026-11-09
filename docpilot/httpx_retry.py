"""Shared HTTP helper: retry 429/5xx with exponential backoff, honouring Retry-After."""

from __future__ import annotations

import asyncio
import random

import httpx

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class UpstreamError(RuntimeError):
    """An upstream API failed. `message` is safe to show to users (no secrets)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _retry_after(resp: httpx.Response) -> float | None:
    """Delay requested by the server: `Retry-After` header, or Google's RetryInfo.retryDelay ("37s")."""
    raw = resp.headers.get("retry-after")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    try:
        body = resp.json()
        details = body.get("error", {}).get("details", []) if isinstance(body, dict) else []
        for d in details:
            if isinstance(d, dict) and str(d.get("@type", "")).endswith("RetryInfo"):
                delay = str(d.get("retryDelay", "")).rstrip("s")
                return max(0.0, float(delay)) if delay else None
    except (ValueError, AttributeError):
        return None
    return None


def is_daily_quota(resp: httpx.Response) -> bool:
    """Google 429s that mention a per-day quota won't clear by waiting a minute."""
    text = resp.text.lower()
    return resp.status_code == 429 and any(k in text for k in ("per day", "perday", "per_day", "_rpd"))


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    json: dict,
    headers: dict[str, str] | None = None,
    retries: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 20.0,
    service: str = "upstream",
) -> dict:
    last_status: int | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.post(url, json=json, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt >= retries:
                raise UpstreamError(f"{service} is unreachable ({type(exc).__name__}).") from exc
            await asyncio.sleep(min(max_delay, base_delay * 2**attempt))
            continue
        if resp.status_code < 400:
            return resp.json()
        last_status = resp.status_code
        if resp.status_code in RETRY_STATUS and attempt < retries and not is_daily_quota(resp):
            delay = _retry_after(resp)
            if delay is None:
                delay = base_delay * 2**attempt + random.uniform(0, 0.25)
            await asyncio.sleep(min(max_delay, delay + random.uniform(0, 0.5)))
            continue
        raise UpstreamError(_error_message(service, resp), resp.status_code)
    raise UpstreamError(f"{service} failed after retries.", last_status)


def _error_message(service: str, resp: httpx.Response) -> str:
    detail = ""
    try:
        body = resp.json()
        err = body.get("error", body) if isinstance(body, dict) else body
        if isinstance(err, dict):
            detail = str(err.get("message") or err.get("detail") or "")
        elif isinstance(err, str):
            detail = err
    except Exception:
        detail = ""
    detail = detail[:300]
    return f"{service} error {resp.status_code}" + (f": {detail}" if detail else "")
