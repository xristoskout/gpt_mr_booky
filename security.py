# file: security.py
from __future__ import annotations

import os
import hashlib
import ipaddress
import time
import threading
from typing import Iterable

from fastapi import Request
from fastapi.responses import JSONResponse

try:
    import redis  # type: ignore
except Exception:  # optional
    redis = None  # type: ignore

from config import Settings

# ──────────────────────────────────────────────────────────────────────────────
# Helpers

def _now() -> int:
    return int(time.time())


def _client_ip(req: Request) -> str:
    xfwd = req.headers.get("x-forwarded-for")
    if xfwd:
        ip = xfwd.split(",")[0].strip()
    else:
        ip = (req.client.host if req.client else "-") or "-"
    try:
        ipaddress.ip_address(ip)
        return ip
    except Exception:
        return "-"


def _origin_headers(req: Request) -> dict:
    # So that browsers can read error bodies in CORS scenarios
    return {
        "Access-Control-Allow-Origin": req.headers.get("origin", "*"),
        "Vary": "Origin",
    }

# ──────────────────────────────────────────────────────────────────────────────
# API Key middleware (ASGI)

class APIKeyAuthMiddleware:
    """Enforce API key on protected routes.
    Accepts X-API-Key or Authorization: Bearer <key>.
    Bypasses preflight (OPTIONS) & public paths so CORS works.
    """

    def __init__(self, app, *, keys: Iterable[str] | None, public_paths: Iterable[str] | None = None):
        self.app = app
        # If keys is None → read CHAT_API_KEYS from env (comma-separated)
        env_keys = {k.strip() for k in (os.getenv("CHAT_API_KEYS", "").split(",")) if k.strip()}
        provided = {k.strip() for k in (keys or []) if k and k.strip()}
        self.keys = provided or env_keys
        self.public = set(public_paths or ["/", "/health", "/docs", "/openapi.json", "/redoc"])  # keep health open

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        # Allow preflight to pass through so CORSMiddleware can answer
        if scope.get("method", "GET").upper() == "OPTIONS":
            return await self.app(scope, receive, send)

        path = scope.get("path", "/")
        if path in self.public or not self.keys:
            # public mode when keys set is empty
            return await self.app(scope, receive, send)

        req = Request(scope, receive)
        key = req.headers.get("x-api-key")
        if not key:
            auth = req.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                key = auth.split(" ", 1)[1].strip()

        if key not in self.keys:
            res = JSONResponse({"detail": "Unauthorized"}, status_code=401, headers=_origin_headers(req))
            return await res(scope, receive, send)

        return await self.app(scope, receive, send)

# ──────────────────────────────────────────────────────────────────────────────
# Rate limiting (fixed window)

class _MemoryBucket:
    __slots__ = ("count", "window_start")

    def __init__(self, count: int, window_start: int):
        self.count = count
        self.window_start = window_start


class MemoryRateLimiter:
    def __init__(self, window_sec: int, max_req: int):
        self.window = int(max(1, window_sec))
        self.max_req = int(max(1, max_req))
        self._lock = threading.Lock()
        self._buckets: dict[str, _MemoryBucket] = {}

    def hit(self, key: str) -> tuple[bool, int]:
        now = _now()
        win = now - (now % self.window)
        with self._lock:
            b = self._buckets.get(key)
            if not b or b.window_start != win:
                b = _MemoryBucket(0, win)
                self._buckets[key] = b
            b.count += 1
            allowed = b.count <= self.max_req
            remaining = max(self.max_req - b.count, 0)
            return allowed, remaining


class RedisRateLimiter:
    def __init__(self, url: str, window_sec: int, max_req: int):
        if redis is None:
            raise RuntimeError("redis library is not installed")
        self.r = redis.Redis.from_url(url, decode_responses=True)
        self.window = int(max(1, window_sec))
        self.max_req = int(max(1, max_req))

    def hit(self, key: str) -> tuple[bool, int]:
        now = _now()
        bucket = now - (now % self.window)
        k = f"rl:{key}:{bucket}"
        with self.r.pipeline() as p:
            p.incr(k)
            p.expire(k, self.window + 1)
            count, _ = p.execute()
        allowed = int(count) <= self.max_req
        remaining = max(self.max_req - int(count), 0)
        return allowed, remaining


class RateLimitMiddleware:
    def __init__(self, app, *, settings: Settings, identifier: str = "auto"):
        self.app = app
        self.settings = settings
        if settings.PERSIST_BACKEND.lower() == "redis" and settings.REDIS_URL:
            self.backend = RedisRateLimiter(settings.REDIS_URL, settings.RATE_LIMIT_WINDOW_SEC, settings.RATE_LIMIT_MAX_REQ)
        else:
            self.backend = MemoryRateLimiter(settings.RATE_LIMIT_WINDOW_SEC, settings.RATE_LIMIT_MAX_REQ)
        self.identifier_mode = identifier

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        # Skip preflight to keep CORS happy
        if scope.get("method", "GET").upper() == "OPTIONS":
            return await self.app(scope, receive, send)

        req = Request(scope, receive)
        if req.method.upper() == "POST" and req.url.path == "/chat":
            ident = await self._identifier(req)
            allowed, remaining = self.backend.hit(ident)
            if not allowed:
                headers = _origin_headers(req)
                headers["Retry-After"] = str(self.settings.RATE_LIMIT_WINDOW_SEC)
                res = JSONResponse({"detail": "Too Many Requests"}, status_code=429, headers=headers)
                return await res(scope, receive, send)
        return await self.app(scope, receive, send)

    async def _identifier(self, req: Request) -> str:
        if self.identifier_mode == "ip":
            return _client_ip(req)
        key = req.headers.get("x-api-key")
        if not key:
            auth = req.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                key = auth.split(" ", 1)[1].strip()
        if key:
            return f"key:{hashlib.sha256(key.encode()).hexdigest()[:16]}"
        return f"ip:{_client_ip(req)}"


# ──────────────────────────────────────────────────────────────────────────────
# Body size limit (Content-Length guard)

class BodySizeLimitMiddleware:
    """Reject requests with Content-Length over max_body_bytes (413).
    Skips GET/HEAD/OPTIONS to avoid breaking browser preflight.
    """

    def __init__(self, app, *, max_body_bytes: int = 1_000_000):
        self.app = app
        self.max = int(max_body_bytes)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        method = scope.get("method", "GET").upper()
        if method in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        clen = headers.get("content-length")
        if clen and clen.isdigit() and int(clen) > self.max:
            # Echo CORS so browser can read the error
            origin = headers.get("origin", "*")
            res = JSONResponse({"detail": "Request body too large"}, status_code=413, headers={
                "Access-Control-Allow-Origin": origin,
                "Vary": "Origin",
            })
            return await res(scope, receive, send)
        return await self.app(scope, receive, send)


# ──────────────────────────────────────────────────────────────────────────────
# Backwards-compatible helpers (exported names used by main.py)

def api_key_auth(app, *, keys: Iterable[str] | None = None, public_paths: Iterable[str] | None = None):
    app.add_middleware(APIKeyAuthMiddleware, keys=keys, public_paths=public_paths)


def rate_limit(app, *, settings: Settings, identifier: str = "auto"):
    app.add_middleware(RateLimitMiddleware, settings=settings, identifier=identifier)


__all__ = [
    "APIKeyAuthMiddleware",
    "RateLimitMiddleware",
    "BodySizeLimitMiddleware",
    "api_key_auth",
    "rate_limit",
]
