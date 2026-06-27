"""
app/api/middleware/rate_limit.py
---------------------------------
Per-API-key rate limiting: 60 requests/minute (architecture §5.1, §8.2).
Uses slowapi (Starlette-compatible limiter backed by Redis or in-memory).
"""

from __future__ import annotations

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import get_settings


def _get_api_key(request: Request) -> str:
    """Key function for slowapi: rate-limit per API key (not IP) when available."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]  # use the token as the key identifier
    return get_remote_address(request)


import redis

settings = get_settings()

# Dual-mode storage URI: Redis with graceful in-memory fallback (architecture §9.4)
storage_uri = "memory://"
if settings.redis_url and settings.redis_url.startswith("redis"):
    try:
        r = redis.from_url(settings.redis_url, socket_connect_timeout=1)
        r.ping()
        storage_uri = settings.redis_url
    except Exception:
        # Redis is down/unavailable; fall back to in-memory storage to prevent crash
        pass

limiter = Limiter(
    key_func=_get_api_key,
    default_limits=[f"{settings.rate_limit_per_minute}/minute"],
    storage_uri=storage_uri,
)
