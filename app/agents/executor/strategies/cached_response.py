"""
app/agents/executor/strategies/cached_response.py
--------------------------------------------------
Strategy: cached_response
Return last known good cached result from Redis (architecture §5.2, §5.3, §6.2).
Used as a fallback when other strategies trigger rate_limit, timeout, network_error, etc.
"""

from __future__ import annotations

import time
from typing import Any
import json

from app.agents.executor.strategies.html_scraping import StrategyError
from app.core.redis_client import get_redis
from app.core.config import get_settings

settings = get_settings()


async def execute(task_payload: dict[str, Any]) -> dict[str, Any]:
    """Retrieve the cached successful result for a task target or task ID.

    Returns:
        dict with items (list[str]), item_count, source_url, strategy
    """
    task_id: str = task_payload.get("task_id", "")
    target_url: str = task_payload.get("target", "")

    start = time.monotonic()

    try:
        client = await get_redis()
    except Exception as exc:
        raise StrategyError(
            "network_error",
            f"Redis unavailable for cache lookup: {exc}"
        ) from exc

    # Try task_id first, then try URL-based cache
    result_data: Any = None
    keys_to_try = []
    if task_id:
        keys_to_try.append(f"agentos:result:{task_id}")
    if target_url:
        keys_to_try.append(f"agentos:result:url:{target_url}")

    for key in keys_to_try:
        try:
            val = await client.get(key)
            if val:
                result_data = json.loads(val)
                break
        except Exception:
            continue

    latency_ms = int((time.monotonic() - start) * 1000)

    if not result_data:
        raise StrategyError(
            "empty_response",
            f"No cached response found for task_id: {task_id} or target: {target_url}"
        )

    # Return structure matching other strategies
    # The cached data should have items, but if it's raw we reconstruct it
    items: list[str] = []
    if isinstance(result_data, dict) and "items" in result_data:
        items = result_data["items"]
    elif isinstance(result_data, list):
        items = result_data
    else:
        items = [str(result_data)]

    return {
        "items": items,
        "item_count": len(items),
        "source_url": target_url,
        "strategy": "cached_response",
        "latency_ms": latency_ms,
        "cached": True,
    }
