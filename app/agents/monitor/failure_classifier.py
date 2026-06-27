"""
app/agents/monitor/failure_classifier.py
----------------------------------------
Rule-based failure classification and confidence scoring (architecture §5.4).
"""

from __future__ import annotations

from typing import Any
from app.core.enums import FailureType


def classify_failure(log_event: dict[str, Any]) -> tuple[FailureType, float, str]:
    """Analyze a log event (specifically an ERROR checkpoint) to determine the failure type,
    confidence score, and evidence text.

    Returns:
        tuple of (FailureType, confidence_score, evidence_string)
    """
    error_type_val = log_event.get("error_type")
    http_status = log_event.get("http_status")
    latency_ms = log_event.get("latency_ms", 0)
    detail = log_event.get("detail", "")
    checkpoint = log_event.get("checkpoint", "")

    # Normalize error type
    if isinstance(error_type_val, str):
        try:
            error_type = FailureType(error_type_val)
        except ValueError:
            error_type = FailureType.UNKNOWN
    else:
        error_type = FailureType.UNKNOWN

    # 1. Check HTTP Status Codes
    if http_status:
        if http_status == 429:
            return (
                FailureType.RATE_LIMIT,
                0.95,
                f"HTTP 429 Rate Limit exceeded at checkpoint {checkpoint} after {latency_ms}ms"
            )
        elif http_status == 408:
            return (
                FailureType.TIMEOUT,
                0.90,
                f"HTTP 408 Timeout received from remote server after {latency_ms}ms"
            )
        elif http_status in (502, 503, 504):
            return (
                FailureType.NETWORK_ERROR,
                0.88,
                f"HTTP {http_status} Server Error from upstream after {latency_ms}ms"
            )

    # 2. Check for explicit error classifications
    if error_type == FailureType.RATE_LIMIT:
        return (
            FailureType.RATE_LIMIT,
            0.92,
            f"Rate limit exception detected: {detail or 'Unknown details'}"
        )

    if error_type == FailureType.TIMEOUT or "timed out" in detail.lower() or "timeout" in detail.lower():
        # Timeout with threshold breach
        confidence = 0.85 if latency_ms > 5000 else 0.80
        return (
            FailureType.TIMEOUT,
            confidence,
            f"Execution timeout: {detail} (elapsed time: {latency_ms}ms)"
        )

    if error_type == FailureType.PARSE_ERROR or "parse" in detail.lower() or "beautifulsoup" in detail.lower():
        # Parse failure
        has_content = "empty" not in detail.lower() and "no items" not in detail.lower()
        confidence = 0.78 if has_content else 0.72
        return (
            FailureType.PARSE_ERROR,
            confidence,
            f"Content parsing failed: {detail}"
        )

    if error_type == FailureType.EMPTY_RESPONSE or "empty" in detail.lower() or "0 items" in detail.lower():
        return (
            FailureType.EMPTY_RESPONSE,
            0.85,
            f"Empty response received: {detail}"
        )

    if error_type == FailureType.NETWORK_ERROR or "connect" in detail.lower() or "unreachable" in detail.lower():
        return (
            FailureType.NETWORK_ERROR,
            0.85,
            f"Network connectivity error: {detail}"
        )

    # 3. Default to Unknown
    return (
        FailureType.UNKNOWN,
        0.50,
        f"Ambiguous failure of type '{error_type_val}' with detail: {detail}"
    )
