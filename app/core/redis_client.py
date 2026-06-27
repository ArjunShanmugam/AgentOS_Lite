"""
app/core/redis_client.py
------------------------
Redis connection factory and Pub/Sub channel definitions.
Used by FastAPI (event publishing), Monitor (RCA publish), and
Supervisor (RCA subscribe + result caching).

Architecture §6.2: cached task results stored in Redis with 1-hour TTL.
Architecture §5.4: Monitor buffers RCA in memory and retries publish every 5s.
"""

from __future__ import annotations

import structlog
import json
from typing import Any
import redis.asyncio as aioredis
from app.core.config import get_settings
logger = structlog.get_logger(__name__)

# ── Channel Names ─────────────────────────────────────────────────────────────
CHANNEL_RCA = "agentos:rca"           # Monitor → Supervisor
CHANNEL_TASK_EVENTS = "agentos:events"  # Executor → SSE broadcaster

# ── Cache Key Patterns ────────────────────────────────────────────────────────
CACHE_RESULT_KEY = "agentos:result:{task_id}"   # 1-hour TTL
CACHE_RESULT_TTL = 3600  # seconds


_redis_pool: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    """Return a lazily-initialised Redis connection pool.
    Raises ConnectionError if Redis is unreachable.
    """
    global _redis_pool
    if _redis_pool is None:
        settings = get_settings()
        _redis_pool = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
    return _redis_pool


async def close_redis() -> None:
    """Gracefully close the Redis connection pool on shutdown."""
    global _redis_pool
    if _redis_pool:
        await _redis_pool.aclose()
        _redis_pool = None


# ── Publish helpers ──────────────────────────────────────────────────────────

async def publish(channel: str, payload: dict[str, Any]) -> bool:
    """Publish a JSON payload to a Redis Pub/Sub channel.
    Returns True on success, False if Redis is unavailable.
    """
    try:
        client = await get_redis()
        await client.publish(channel, json.dumps(payload, default=str))
        return True
    except Exception as exc:
        logger.warning("redis_publish_failed", channel=channel, error=str(exc))
        return False


async def publish_with_retry(
    channel: str,
    payload: dict[str, Any],
    max_retries: int = 5,
    interval_seconds: int = 5,
) -> bool:
    """Publish with linear backoff retry (architecture §9.2).
    Used by Monitor when Redis is temporarily unavailable.
    """
    for attempt in range(1, max_retries + 1):
        if await publish(channel, payload):
            return True
        logger.warning(
            "redis_publish_retry",
            attempt=attempt,
            max_retries=max_retries,
            channel=channel,
        )
        if attempt < max_retries:
            await asyncio.sleep(interval_seconds)
    logger.error("redis_publish_exhausted", channel=channel, max_retries=max_retries)
    return False


# ── Cache helpers ────────────────────────────────────────────────────────────

async def cache_result(task_id: str, result: Any) -> None:
    """Store a task result in Redis with 1-hour TTL (architecture §6.2)."""
    try:
        client = await get_redis()
        key = CACHE_RESULT_KEY.format(task_id=task_id)
        await client.set(key, json.dumps(result, default=str), ex=CACHE_RESULT_TTL)
    except Exception as exc:
        logger.warning("redis_cache_write_failed", task_id=task_id, error=str(exc))


async def get_cached_result(task_id: str) -> Any | None:
    """Retrieve a cached task result. Returns None if not found or Redis unavailable."""
    try:
        client = await get_redis()
        key = CACHE_RESULT_KEY.format(task_id=task_id)
        value = await client.get(key)
        return json.loads(value) if value else None
    except Exception as exc:
        logger.warning("redis_cache_read_failed", task_id=task_id, error=str(exc))
        return None
