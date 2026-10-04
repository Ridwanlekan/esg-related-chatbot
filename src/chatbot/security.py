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


def trusted_proxies():
    """Peer addresses whose X-Forwarded-For header we are willing to believe."""
    raw = os.environ.get("TRUSTED_PROXY_IPS", "")
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def client_ip(request):
    """Best-effort caller address for rate-limit keying.

    When TLS is terminated in front of the app, `request.client.host` is the
    proxy, not the user, so every caller collapses into one bucket. uvicorn's
    own proxy_headers handling rewrites the scope client for the standard
    deployment; this is the app-level fallback for deployments that terminate
    elsewhere.

    X-Forwarded-For is honoured ONLY from a peer listed in TRUSTED_PROXY_IPS.
    Trusting it unconditionally would let any client mint a fresh rate-limit
    bucket per request by sending a random header, which is strictly worse than
    the shared-bucket bug it fixes.
    """
    peer = request.client.host if request.client else None
    allowed = trusted_proxies()
    if not peer or ("*" not in allowed and peer not in allowed):
        return peer or "unknown"
    for candidate in (c.strip() for c in request.headers.get("x-forwarded-for", "").split(",")):
        if candidate:
            return candidate
    return peer


def make_rate_limit(limiter, requests, window_seconds):
    """Return a FastAPI dependency enforcing a per-caller sliding-window cap."""
    if not requests:
        return None

    def check(request: Request):
        key = client_ip(request)
        if not limiter.allow(f"{key}:{request.url.path}"):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")

    return check


def int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default