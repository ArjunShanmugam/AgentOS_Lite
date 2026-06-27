"""
app/agents/executor/strategies/html_scraping.py
------------------------------------------------
Strategy: html_scraping
Direct HTTP fetch + BeautifulSoup HTML parse (architecture §5.2).

Raises:
  RateLimitError  — HTTP 429
  TimeoutError    — request > task_timeout_seconds
  ParseError      — HTML parse failure / empty result
  NetworkError    — connection failure
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.core.config import get_settings

settings = get_settings()


class StrategyError(Exception):
    """Base for all strategy-level errors."""
    def __init__(self, error_type: str, detail: str, http_status: int | None = None):
        self.error_type = error_type
        self.detail = detail
        self.http_status = http_status
        super().__init__(detail)


async def execute(task_payload: dict[str, Any]) -> dict[str, Any]:
    """Fetch and parse an HTML page.

    Returns:
        dict with keys: items (list[str]), item_count (int), source_url (str)
    Raises:
        StrategyError with error_type set to the failure classification.
    """
    target_url: str = task_payload.get("target", "")
    max_items: int = task_payload.get("max_items", 10)
    output_format: str = task_payload.get("output_format", "summary_list")

    if not target_url:
        raise StrategyError("parse_error", "No target URL provided")

    start = time.monotonic()

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.task_timeout_seconds),
            follow_redirects=True,
            headers={"User-Agent": "AgentOS-Lite/1.0 (research; non-commercial)"},
        ) as client:
            response = await client.get(target_url)
            latency_ms = int((time.monotonic() - start) * 1000)

            if response.status_code == 429:
                raise StrategyError(
                    "rate_limit",
                    f"HTTP 429 received from {target_url}",
                    http_status=429,
                )
            if response.status_code >= 500:
                raise StrategyError(
                    "network_error",
                    f"Server error HTTP {response.status_code}",
                    http_status=response.status_code,
                )
            response.raise_for_status()

    except httpx.TimeoutException as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        raise StrategyError(
            "timeout",
            f"Request timed out after {latency_ms}ms ({target_url})",
        ) from exc
    except httpx.ConnectError as exc:
        raise StrategyError("network_error", f"Connection failed: {exc}") from exc
    except StrategyError:
        raise
    except httpx.HTTPStatusError as exc:
        raise StrategyError(
            "network_error",
            f"HTTP error: {exc}",
            http_status=exc.response.status_code,
        ) from exc

    # ── Parse HTML ────────────────────────────────────────────────────────────
    try:
        soup = BeautifulSoup(response.text, "html.parser")

        # Generic extraction: find all anchor texts + titles as "items"
        items: list[str] = []

        # Strategy: look for <title>, <h1>–<h3>, <a> with meaningful text
        for tag in soup.find_all(["h1", "h2", "h3", "a", "p"]):
            text = tag.get_text(strip=True)
            if text and len(text) > 10:
                items.append(text)
            if len(items) >= max_items * 3:
                break

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_items: list[str] = []
        for item in items:
            if item not in seen:
                seen.add(item)
                unique_items.append(item)
        items = unique_items[:max_items]

        if not items:
            raise StrategyError(
                "empty_response",
                f"HTML parse returned no content from {target_url}",
            )

    except StrategyError:
        raise
    except Exception as exc:
        raise StrategyError("parse_error", f"HTML parse failed: {exc}") from exc

    return {
        "items": items,
        "item_count": len(items),
        "source_url": target_url,
        "strategy": "html_scraping",
        "latency_ms": latency_ms,
    }
