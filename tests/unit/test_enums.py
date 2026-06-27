"""
tests/unit/test_enums.py
------------------------
Unit tests for canonical enumerations and static data structures in app/core/enums.py.
Verifies:
  - All StrategyEnum values are valid strings
  - All FailureType values are present
  - RECOVERY_MAP covers all FailureType keys
  - RECOVERY_MAP values only reference valid StrategyEnum entries
  - VALID_TRANSITIONS covers all TaskStatus values
  - Terminal states have no outgoing transitions (INV-08)
  - TaskStatus.is_terminal property is correct
  - Redis channel constants are non-empty strings
"""

from __future__ import annotations

import pytest

from app.core.enums import (
    Checkpoint,
    CircuitBreakerState,
    FailureType,
    RECOVERY_MAP,
    StrategyEnum,
    TaskStatus,
    VALID_TRANSITIONS,
)
from app.core.redis_client import CHANNEL_RCA, CHANNEL_TASK_EVENTS


# ── StrategyEnum ─────────────────────────────────────────────────────────────

class TestStrategyEnum:
    def test_all_four_strategies_present(self):
        values = {s.value for s in StrategyEnum}
        assert values == {"html_scraping", "rss_fallback", "api_fallback", "cached_response"}

    def test_strategy_is_str_subclass(self):
        for s in StrategyEnum:
            assert isinstance(s, str), f"{s} should be a str subclass"

    def test_strategy_values_are_lowercase_underscored(self):
        for s in StrategyEnum:
            assert s.value == s.value.lower()
            assert " " not in s.value


# ── TaskStatus ───────────────────────────────────────────────────────────────

class TestTaskStatus:
    def test_all_six_statuses_present(self):
        values = {s.value for s in TaskStatus}
        assert values == {
            "PENDING", "RUNNING", "RECOVERING",
            "COMPLETE", "FAILED_PERMANENT", "FAILED_MAX_RETRIES"
        }

    def test_terminal_statuses(self):
        assert TaskStatus.COMPLETE.is_terminal is True
        assert TaskStatus.FAILED_PERMANENT.is_terminal is True
        assert TaskStatus.FAILED_MAX_RETRIES.is_terminal is True

    def test_non_terminal_statuses(self):
        assert TaskStatus.PENDING.is_terminal is False
        assert TaskStatus.RUNNING.is_terminal is False
        assert TaskStatus.RECOVERING.is_terminal is False


# ── VALID_TRANSITIONS ────────────────────────────────────────────────────────

class TestValidTransitions:
    def test_all_statuses_have_entries(self):
        """Every TaskStatus must have an entry in VALID_TRANSITIONS."""
        for status in TaskStatus:
            assert status in VALID_TRANSITIONS, f"{status} missing from VALID_TRANSITIONS"

    def test_terminal_statuses_have_empty_transitions(self):
        """INV-08: terminal states must have no outgoing transitions."""
        for status in (TaskStatus.COMPLETE, TaskStatus.FAILED_PERMANENT, TaskStatus.FAILED_MAX_RETRIES):
            assert VALID_TRANSITIONS[status] == set(), (
                f"Terminal status {status} should have no transitions but has: {VALID_TRANSITIONS[status]}"
            )

    def test_pending_can_only_go_to_running(self):
        assert VALID_TRANSITIONS[TaskStatus.PENDING] == {TaskStatus.RUNNING}

    def test_running_can_reach_terminal_or_recovering(self):
        expected = {
            TaskStatus.RECOVERING,
            TaskStatus.COMPLETE,
            TaskStatus.FAILED_PERMANENT,
            TaskStatus.FAILED_MAX_RETRIES,
        }
        assert VALID_TRANSITIONS[TaskStatus.RUNNING] == expected

    def test_recovering_can_reach_expected_targets(self):
        targets = VALID_TRANSITIONS[TaskStatus.RECOVERING]
        assert TaskStatus.RUNNING in targets
        assert TaskStatus.COMPLETE in targets
        # All targets must be valid TaskStatus values
        for t in targets:
            assert isinstance(t, TaskStatus)


# ── FailureType ──────────────────────────────────────────────────────────────

class TestFailureType:
    def test_all_six_failure_types_present(self):
        values = {f.value for f in FailureType}
        assert values == {
            "rate_limit", "timeout", "parse_error",
            "empty_response", "network_error", "unknown"
        }

    def test_failure_type_is_str(self):
        for f in FailureType:
            assert isinstance(f, str)


# ── RECOVERY_MAP ─────────────────────────────────────────────────────────────

class TestRecoveryMap:
    def test_all_failure_types_covered(self):
        """Every FailureType must have an entry in RECOVERY_MAP (INV-03)."""
        for ft in FailureType:
            assert ft in RECOVERY_MAP, f"{ft} missing from RECOVERY_MAP"

    def test_all_recovery_strategies_are_valid(self):
        """RECOVERY_MAP values must only contain valid StrategyEnum entries."""
        valid_strategies = set(StrategyEnum)
        for ft, strategies in RECOVERY_MAP.items():
            assert len(strategies) > 0, f"RECOVERY_MAP[{ft}] must not be empty"
            for s in strategies:
                assert s in valid_strategies, f"Invalid strategy {s!r} in RECOVERY_MAP[{ft}]"

    def test_rate_limit_excludes_html_scraping(self):
        """HTML scraping cannot be a recovery for rate_limit (it IS the failing strategy)."""
        assert StrategyEnum.HTML_SCRAPING not in RECOVERY_MAP[FailureType.RATE_LIMIT]

    def test_network_error_uses_cached_response(self):
        """Network errors fall back only to cached response."""
        assert RECOVERY_MAP[FailureType.NETWORK_ERROR] == [StrategyEnum.CACHED_RESPONSE]

    def test_parse_error_uses_rss_and_api(self):
        strategies = RECOVERY_MAP[FailureType.PARSE_ERROR]
        assert StrategyEnum.RSS_FALLBACK in strategies
        assert StrategyEnum.API_FALLBACK in strategies


# ── Checkpoint ───────────────────────────────────────────────────────────────

class TestCheckpoint:
    def test_all_five_checkpoints_present(self):
        values = {c.value for c in Checkpoint}
        assert values == {"START", "FETCH", "PARSE", "COMPLETE", "ERROR"}


# ── CircuitBreakerState ──────────────────────────────────────────────────────

class TestCircuitBreakerState:
    def test_three_states_present(self):
        values = {s.value for s in CircuitBreakerState}
        assert values == {"CLOSED", "OPEN", "HALF_OPEN"}


# ── Redis Channel Constants ──────────────────────────────────────────────────

class TestRedisChannels:
    def test_channel_rca_is_nonempty_string(self):
        assert isinstance(CHANNEL_RCA, str)
        assert len(CHANNEL_RCA) > 0

    def test_channel_task_events_is_nonempty_string(self):
        assert isinstance(CHANNEL_TASK_EVENTS, str)
        assert len(CHANNEL_TASK_EVENTS) > 0

    def test_channels_are_different(self):
        assert CHANNEL_RCA != CHANNEL_TASK_EVENTS
