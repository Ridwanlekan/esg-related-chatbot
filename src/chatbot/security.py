import os
import secrets
import time

from fastapi import Header, HTTPException, Request


class RateLimiter:
    """Sliding-window rate limiter keyed by caller identity."""

    def __init__(self, max_requests, window_seconds):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits = {}

    def allow(self, key):
        now = time.monotonic()
        hits = self._hits.setdefault(key, [])
        cutoff = now - self.window_seconds
        while hits and hits[0] <= cutoff:
            hits.pop(0)
        hits.append(now)
        return len(hits) <= self.max_requests

    def reset(self, key=None):
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)


def make_auth_check(api_key):
    """Return a FastAPI dependency enforcing `Authorization: Bearer <api_key>`.

    When `api_key` is empty the dependency is disabled (dev mode); the app
    logs a warning instead so local workflows stay friction-free.
    """
    if not api_key:
        return None
    expected = f"Bearer {api_key}"

    async def check(authorization: str | None = Header(default=None)):
        if authorization is None or not secrets.compare_digest(
            authorization, expected
        ):
            raise HTTPException(status_code=401, detail="Invalid or missing API key")

    return check


def make_rate_limit(limiter, requests, window_seconds):
    """Return a FastAPI dependency enforcing a per-IP sliding-window cap."""
    if not requests:
        return None

    def check(request: Request):
        key = request.client.host if request.client else "unknown"
        if not limiter.allow(f"{key}:{request.url.path}"):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")

    return check


def int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default