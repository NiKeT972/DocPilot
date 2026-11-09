"""Per-IP rate limiting backed by the store's `usage_events` (works across Vercel instances)."""

from __future__ import annotations

import hashlib
import hmac

from fastapi import Request

from .store import Store

HOUR = 3600
DAY = 86400


class RateLimited(Exception):
    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


def client_ip(request: Request) -> str:
    # Vercel sets x-real-ip / x-forwarded-for itself (client-supplied values are overwritten).
    for header in ("x-real-ip", "x-forwarded-for"):
        raw = request.headers.get(header)
        if raw:
            return raw.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def hash_ip(ip: str, salt: str) -> str:
    return hmac.new(salt.encode(), ip.encode(), hashlib.sha256).hexdigest()[:32]


class RateLimiter:
    def __init__(self, store: Store, salt: str):
        self.store = store
        self.salt = salt

    async def hit(
        self,
        request: Request,
        kind: str,
        limit: int,
        window: int,
        session_id: str | None = None,
        cost: int = 1,
        friendly: str = "",
    ) -> None:
        """Count `cost` usage units for this IP, or raise RateLimited if that would exceed `limit`."""
        if limit <= 0:
            return
        ip_hash = hash_ip(client_ip(request), self.salt)
        used = await self.store.count_events(ip_hash, kind, window)
        if used + cost > limit:
            period = "hour" if window <= HOUR else "day"
            raise RateLimited(
                friendly
                or f"You've reached the limit of {limit} {kind} requests per {period}. Please try again later.",
                retry_after=window if window <= HOUR else HOUR,
            )
        for _ in range(cost):
            await self.store.record_event(ip_hash, session_id, kind)
