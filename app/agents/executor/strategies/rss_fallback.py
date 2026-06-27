"""
app/agents/executor/strategies/rss_fallback.py
----------------------------------------------
Strategy: rss_fallback
RSS/Atom feed parse via feedparser (architecture §5.2).
Used when html_scraping triggers rate_limit or parse_error.

The task payload's `rss_feed_url` field is used as the feed target.
Falls back to common RSS patterns for known domains if not provided.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser
import httpx

from app.agents.executor.strategies.html_scraping import StrategyError
from app.core.config import get_settings

settings = get_settings()

# Common RSS/Atom URL suffixes to probe when rss_feed_url not specified
_RSS_SUFFIXES = ["/rss", "/feed", "/rss.xml", "/atom.xml", "/feed.xml", "/rss/all"]


def _guess_rss_url(base_url: str) -> list[str]:
    """Derive candidate RSS URLs from a base page URL."""
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    return [urljoin(root, suffix) for suffix in _RSS_SUFFIXES]


async def _fetch_bytes(url: str) -> tuple[bytes, int]:
    """Download content and return (bytes, latency_ms)."""
    start = time.monotonic()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(settings.task_timeout_seconds),
        follow_redirects=True,
        headers={"User-Agent": "AgentOS-Lite/1.0 (research; non-commercial)"},
    ) as client:
        response = await client.get(url)
        latency_ms = int((time.monotonic() - start) * 1000)
        if response.status_code == 429:
            raise StrategyError("rate_limit", f"HTTP 429 from {url}", http_status=429)
        response.raise_for_status()
        return response.content, latency_ms


async def execute(task_payload: dict[str, Any]) -> dict[str, Any]:
    """Parse an RSS/Atom feed and return structured item list.

    Returns:
        dict with items (list[str]), item_count, source_url, strategy
    """
    target_url: str = task_payload.get("target", "")
    rss_url: str | None = task_payload.get("rss_feed_url")
    max_items: int = task_payload.get("max_items", 10)

    # Determine feed URLs to try
    candidates: list[str] = []
    if rss_url:
        candidates.append(rss_url)
    candidates.extend(_guess_rss_url(target_url))

    last_error: StrategyError | None = None
    latency_ms = 0

    for feed_url in candidates:
        try:
            content, latency_ms = await _fetch_bytes(feed_url)
            feed = feedparser.parse(content)

            if feed.bozo and not feed.entries:
                last_error = StrategyError(
                    "parse_error",
                    f"feedparser could not parse {feed_url}: {feed.bozo_exception}",
                )
                continue

            entries = feed.entries[:max_items]
            if not entries:
                last_error = StrategyError(
                    "empty_response",
                    f"RSS feed at {feed_url} returned 0 entries",
                )
                continue

            items = []
            for entry in entries:
                title = entry.get("title", "").strip()
                summary = entry.get("summary", entry.get("description", "")).strip()
                link = entry.get("link", "")
                if title:
                    items.append(f"{title} — {summary[:100]}" if summary else title)

            return {
                "items": items,
                "item_count": len(items),
                "source_url": feed_url,
                "feed_title": feed.feed.get("title", ""),
                "strategy": "rss_fallback",
                "latency_ms": latency_ms,
            }

        except StrategyError:
            raise
        except httpx.TimeoutException:
            last_error = StrategyError("timeout", f"RSS fetch timed out: {feed_url}")
            continue
        except httpx.ConnectError as exc:
            last_error = StrategyError("network_error", f"RSS connection failed: {exc}")
            continue
        except Exception as exc:
            last_error = StrategyError("parse_error", f"RSS error at {feed_url}: {exc}")
            continue

    raise last_error or StrategyError(
        "empty_response",
        f"No valid RSS feed found for {target_url}",
    )
