"""
tests/unit/test_failure_classifier.py
-------------------------------------
Unit tests for the rule-based failure classifier.
"""

from __future__ import annotations

from app.core.enums import FailureType
from app.agents.monitor.failure_classifier import classify_failure


def test_classify_http_429():
    log_event = {
        "checkpoint": "FETCH",
        "status": "ERROR",
        "error_type": "rate_limit",
        "http_status": 429,
        "latency_ms": 312,
        "detail": "Rate limit reached"
    }
    ftype, confidence, evidence = classify_failure(log_event)
    assert ftype == FailureType.RATE_LIMIT
    assert confidence == 0.95
    assert "HTTP 429" in evidence


def test_classify_timeout():
    log_event = {
        "checkpoint": "FETCH",
        "status": "ERROR",
        "error_type": "timeout",
        "latency_ms": 11200,
        "detail": "Request timed out"
    }
    ftype, confidence, evidence = classify_failure(log_event)
    assert ftype == FailureType.TIMEOUT
    assert confidence == 0.85  # latency > 5000
    assert "timeout" in evidence.lower()


def test_classify_parse_error():
    log_event = {
        "checkpoint": "PARSE",
        "status": "ERROR",
        "error_type": "parse_error",
        "detail": "BeautifulSoup could not find table element"
    }
    ftype, confidence, evidence = classify_failure(log_event)
    assert ftype == FailureType.PARSE_ERROR
    assert confidence == 0.78
    assert "parsing failed" in evidence
