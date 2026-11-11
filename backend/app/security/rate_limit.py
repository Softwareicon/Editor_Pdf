"""In-process sliding-window rate limiter.

Sufficient for a single API process. With several workers/hosts the counters must move to
Redis (planned alongside the Phase 3 job queue); the interface here will not change.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import Request

from ..config import get_settings
from ..errors import ApiError


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window: float = 60.0) -> float | None:
        """Record a hit. Returns None if allowed, else seconds until the next slot frees up."""
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return max(0.0, q[0] + window - now)
            q.append(now)
            if len(self._hits) > 50_000:  # bound memory under address-spraying
                for k in [k for k, v in self._hits.items() if not v or v[-1] <= now - window]:
                    del self._hits[k]
            return None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


auth_limiter = SlidingWindowLimiter()


def limit_auth(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    retry = auth_limiter.check(f"{request.url.path}:{ip}", get_settings().auth_rate_limit_per_minute)
    if retry is not None:
        raise ApiError(429, "rate_limited", "Too many attempts. Please wait and try again.",
                       {"Retry-After": str(int(retry) + 1)})
