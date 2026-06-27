"""
tests/unit/test_config_validator.py
------------------------------------
Unit tests for ExecutorConfig and related Pydantic schema validation.
Verifies that:
  - Valid YAML-equivalent dicts parse correctly into ExecutorConfig
  - Invalid strategy values are rejected
  - Missing required fields raise validation errors
  - SupervisorDecision validates LLM output correctly (§8.5 four-step validation)
  - RCAReport rejects empty suggested_strategies list
  - TaskRequest field constraints are enforced (max_items, empty target)
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.enums import StrategyEnum, FailureType, Checkpoint
from app.core.schemas import (
    ExecutorConfig,
    RCAReport,
    SupervisorDecision,
    TaskRequest,
    ExecutorLogEvent,
)


# ── ExecutorConfig ────────────────────────────────────────────────────────────

class TestExecutorConfig:
    def test_valid_html_scraping_config(self):
        cfg = ExecutorConfig(
            agent_id="executor-01",
            strategy=StrategyEnum.HTML_SCRAPING,
            schema_version=1,
        )
        assert cfg.agent_id == "executor-01"
        assert cfg.strategy == StrategyEnum.HTML_SCRAPING
        assert cfg.schema_version == 1

    def test_all_strategy_values_accepted(self):
        for strategy in StrategyEnum:
            cfg = ExecutorConfig(agent_id="executor-01", strategy=strategy)
            assert cfg.strategy == strategy

    def test_invalid_strategy_raises(self):
        with pytest.raises(ValidationError):
            ExecutorConfig(agent_id="executor-01", strategy="not_a_real_strategy")

    def test_missing_agent_id_raises(self):
        with pytest.raises(ValidationError):
            ExecutorConfig(strategy=StrategyEnum.HTML_SCRAPING)

    def test_default_schema_version_is_1(self):
        cfg = ExecutorConfig(agent_id="executor-01", strategy=StrategyEnum.RSS_FALLBACK)
        assert cfg.schema_version == 1

    def test_strategy_parsed_from_string(self):
        """Config loaded from YAML arrives as raw strings — Pydantic must coerce."""
        cfg = ExecutorConfig.model_validate({
            "agent_id": "executor-01",
            "strategy": "rss_fallback",
            "schema_version": 2,
        })
        assert cfg.strategy == StrategyEnum.RSS_FALLBACK
        assert cfg.schema_version == 2

    def test_cached_response_strategy_valid(self):
        cfg = ExecutorConfig.model_validate({
            "agent_id": "executor-01",
            "strategy": "cached_response",
        })
        assert cfg.strategy == StrategyEnum.CACHED_RESPONSE


# ── SupervisorDecision ────────────────────────────────────────────────────────

class TestSupervisorDecision:
    def test_valid_decision(self):
        dec = SupervisorDecision(
            strategy=StrategyEnum.RSS_FALLBACK,
            rationale="Rate limit detected; switching to RSS feed.",
        )
        assert dec.strategy == StrategyEnum.RSS_FALLBACK
        assert len(dec.rationale) > 0

    def test_invalid_strategy_string_rejected(self):
        with pytest.raises(ValidationError):
            SupervisorDecision(strategy="arbitrary_code_execution", rationale="test")

    def test_missing_rationale_raises(self):
        with pytest.raises(ValidationError):
            SupervisorDecision(strategy=StrategyEnum.API_FALLBACK)

    def test_all_valid_strategies_accepted(self):
        for strategy in StrategyEnum:
            dec = SupervisorDecision(strategy=strategy, rationale="test rationale")
            assert dec.strategy == strategy


# ── RCAReport ─────────────────────────────────────────────────────────────────

class TestRCAReport:
    def test_valid_rca_report(self):
        rca = RCAReport(
            task_id="test-task-001",
            failure_type=FailureType.RATE_LIMIT,
            evidence="HTTP 429 at checkpoint FETCH after 312ms",
            confidence=0.92,
            suggested_strategies=[StrategyEnum.RSS_FALLBACK, StrategyEnum.API_FALLBACK],
            health_score=0.38,
        )
        assert rca.failure_type == FailureType.RATE_LIMIT
        assert rca.confidence == pytest.approx(0.92)
        assert len(rca.suggested_strategies) == 2

    def test_empty_suggested_strategies_rejected(self):
        with pytest.raises(ValidationError):
            RCAReport(
                task_id="test-task-001",
                failure_type=FailureType.TIMEOUT,
                evidence="Timeout at FETCH",
                confidence=0.8,
                suggested_strategies=[],  # must not be empty
                health_score=0.5,
            )

    def test_confidence_above_1_rejected(self):
        with pytest.raises(ValidationError):
            RCAReport(
                task_id="test-task-001",
                failure_type=FailureType.PARSE_ERROR,
                evidence="Parse failure",
                confidence=1.5,   # > 1.0 is invalid
                suggested_strategies=[StrategyEnum.RSS_FALLBACK],
                health_score=0.5,
            )

    def test_health_score_below_0_rejected(self):
        with pytest.raises(ValidationError):
            RCAReport(
                task_id="test-task-001",
                failure_type=FailureType.UNKNOWN,
                evidence="Unknown failure",
                confidence=0.4,
                suggested_strategies=[StrategyEnum.CACHED_RESPONSE],
                health_score=-0.1,  # < 0.0 is invalid
            )


# ── TaskRequest ───────────────────────────────────────────────────────────────

class TestTaskRequest:
    def test_valid_task_request(self):
        req = TaskRequest(
            task_type="web_scrape",
            target="https://news.ycombinator.com",
            max_items=10,
        )
        assert req.task_type == "web_scrape"
        assert req.max_items == 10

    def test_max_items_upper_bound(self):
        """max_items must not exceed 50."""
        with pytest.raises(ValidationError):
            TaskRequest(task_type="web_scrape", target="https://example.com", max_items=51)

    def test_max_items_lower_bound(self):
        """max_items must be at least 1."""
        with pytest.raises(ValidationError):
            TaskRequest(task_type="web_scrape", target="https://example.com", max_items=0)

    def test_empty_target_rejected(self):
        """target must not be empty or whitespace only."""
        with pytest.raises(ValidationError):
            TaskRequest(task_type="web_scrape", target="   ")

    def test_target_is_stripped(self):
        req = TaskRequest(task_type="web_scrape", target="  https://example.com  ")
        assert req.target == "https://example.com"

    def test_optional_rss_field(self):
        req = TaskRequest(
            task_type="web_scrape",
            target="https://example.com",
            rss_feed_url="https://example.com/feed.rss",
        )
        assert req.rss_feed_url == "https://example.com/feed.rss"

    def test_optional_api_endpoint(self):
        req = TaskRequest(
            task_type="web_scrape",
            target="https://example.com",
            api_endpoint="https://api.example.com/v1/data",
        )
        assert req.api_endpoint == "https://api.example.com/v1/data"


# ── ExecutorLogEvent ──────────────────────────────────────────────────────────

class TestExecutorLogEvent:
    def test_valid_error_event_requires_error_type(self):
        """INV-10: error checkpoint must include error_type."""
        evt = ExecutorLogEvent(
            task_id="abc-123",
            agent_id="executor-01",
            strategy=StrategyEnum.HTML_SCRAPING,
            checkpoint=Checkpoint.ERROR,
            status="ERROR",
            error_type=FailureType.RATE_LIMIT,
            http_status=429,
            latency_ms=312,
        )
        assert evt.error_type == FailureType.RATE_LIMIT

    def test_error_checkpoint_without_error_type_rejected(self):
        """If checkpoint is ERROR, error_type must be present."""
        with pytest.raises(ValidationError):
            ExecutorLogEvent(
                task_id="abc-123",
                agent_id="executor-01",
                strategy=StrategyEnum.HTML_SCRAPING,
                checkpoint=Checkpoint.ERROR,
                status="ERROR",
                # error_type is missing!
            )

    def test_valid_complete_event(self):
        evt = ExecutorLogEvent(
            task_id="abc-123",
            agent_id="executor-01",
            strategy=StrategyEnum.RSS_FALLBACK,
            checkpoint=Checkpoint.COMPLETE,
            status="OK",
            latency_ms=450,
        )
        assert evt.checkpoint == Checkpoint.COMPLETE
        assert evt.error_type is None
