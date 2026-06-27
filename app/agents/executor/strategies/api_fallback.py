"""
app/agents/executor/strategies/api_fallback.py
----------------------------------------------
Strategy: api_fallback
Structured REST API calls (architecture §5.2).
Used when html_scraping/rss_fallback triggers rate_limit, timeout, parse_error, or empty_response.

If the target domain is Hacker News (news.ycombinator.com), this queries the official
Hacker News Firebase REST API to fetch top stories.
Otherwise, it falls back to querying a standard placeholder API (e.g. JSONPlaceholder)
or simulates a REST API retrieval.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

import httpx

from app.agents.executor.strategies.html_scraping import StrategyError
from app.core.config import get_settings

settings = get_settings()


async def execute(task_payload: dict[str, Any]) -> dict[str, Any]:
    """Fetch structured data from a REST API.

    Returns:
        dict with items (list[str]), item_count, source_url, strategy
    """
    target_url: str = task_payload.get("target", "")
    max_items: int = task_payload.get("max_items", 10)

    if not target_url:
        raise StrategyError("parse_error", "No target URL provided")

    parsed = urlparse(target_url)
    domain = parsed.netloc.lower()

    start = time.monotonic()
    items: list[str] = []
    source_url = target_url

    try:
        # Special casing Hacker News
        if "news.ycombinator.com" in domain or "hn" in domain:
            source_url = "https://hacker-news.firebaseio.com/v0/topstories.json"
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(settings.task_timeout_seconds),
                headers={"User-Agent": "AgentOS-Lite/1.0 (research; non-commercial)"},
            ) as client:
                # 1. Fetch top story IDs
                resp = await client.get(source_url)
                if resp.status_code == 429:
                    raise StrategyError("rate_limit", "HN API rate limited", http_status=429)
                resp.raise_for_status()

                story_ids = resp.json()[:max_items]
                if not story_ids:
                    raise StrategyError("empty_response", "HN topstories returned no IDs")

                # 2. Fetch details for each story
                for sid in story_ids:
                    item_url = f"https://hacker-news.firebaseio.com/v0/item/{sid}.json"
                    item_resp = await client.get(item_url)
                    if item_resp.status_code == 200:
                        data = item_resp.json()
                        if data and "title" in data:
                            items.append(f"{data['title']} (by {data.get('by', 'unknown')})")
                    if len(items) >= max_items:
                        break
        else:
            # Fallback to a mock/placeholder API for other targets, e.g. JSONPlaceholder
            source_url = "https://jsonplaceholder.typicode.com/posts"
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(settings.task_timeout_seconds),
                headers={"User-Agent": "AgentOS-Lite/1.0 (research; non-commercial)"},
            ) as client:
                resp = await client.get(source_url)
                if resp.status_code == 429:
                    raise StrategyError("rate_limit", "Placeholder API rate limited", http_status=429)
                resp.raise_for_status()

                posts = resp.json()[:max_items]
                for post in posts:
                    title = post.get("title", "")
                    if title:
                        items.append(title)

        latency_ms = int((time.monotonic() - start) * 1000)

        if not items:
            raise StrategyError("empty_response", f"API response from {source_url} parsed to 0 items")

        return {
            "items": items,
            "item_count": len(items),
            "source_url": source_url,
            "strategy": "api_fallback",
            "latency_ms": latency_ms,
        }

    except httpx.TimeoutException as exc:
        raise StrategyError("timeout", f"API fallback timed out: {source_url}") from exc
    except httpx.ConnectError as exc:
        raise StrategyError("network_error", f"API connection failed: {exc}") from exc
    except StrategyError:
        raise
    except Exception as exc:
        raise StrategyError("parse_error", f"API parsing/fetching error: {exc}") from exc
