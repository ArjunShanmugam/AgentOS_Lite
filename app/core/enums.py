"""
app/core/enums.py
-----------------
Canonical enumerations used across every agent and API layer.
All strategy selection, status tracking, and failure classification
flows through these enums — never raw strings.

Architecture invariants enforced here:
  INV-02: Supervisor only selects from StrategyEnum.
  INV-03: Checked against recovery_map at selection time.
"""

from enum import Enum


class StrategyEnum(str, Enum):
    """Bounded set of retrieval strategies available to the Executor.
    The Supervisor may ONLY select from this enum (INV-02).
    Adding a new strategy requires a code change + schema migration.
    """
    HTML_SCRAPING = "html_scraping"
    RSS_FALLBACK = "rss_fallback"
    API_FALLBACK = "api_fallback"
    CACHED_RESPONSE = "cached_response"


class TaskStatus(str, Enum):
    """Legal task lifecycle states.
    Transitions are strictly one-way; COMPLETE and FAILED_* are terminal (INV-08).
    """
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    RECOVERING = "RECOVERING"
    COMPLETE = "COMPLETE"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    FAILED_MAX_RETRIES = "FAILED_MAX_RETRIES"

    @property
    def is_terminal(self) -> bool:
        return self in (
            TaskStatus.COMPLETE,
            TaskStatus.FAILED_PERMANENT,
            TaskStatus.FAILED_MAX_RETRIES,
        )


# Valid status transitions (INV-08: no transition OUT of terminal states)
VALID_TRANSITIONS: dict["TaskStatus", set["TaskStatus"]] = {
    TaskStatus.PENDING: {TaskStatus.RUNNING},
    TaskStatus.RUNNING: {TaskStatus.RECOVERING, TaskStatus.COMPLETE, TaskStatus.FAILED_PERMANENT, TaskStatus.FAILED_MAX_RETRIES},
    TaskStatus.RECOVERING: {TaskStatus.RUNNING, TaskStatus.COMPLETE, TaskStatus.FAILED_PERMANENT, TaskStatus.FAILED_MAX_RETRIES},
    TaskStatus.COMPLETE: set(),
    TaskStatus.FAILED_PERMANENT: set(),
    TaskStatus.FAILED_MAX_RETRIES: set(),
}


class FailureType(str, Enum):
    """Failure types detected by the Monitor's rule-based classifier.
    Each maps to an allowed recovery strategy set in RECOVERY_MAP.
    """
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    PARSE_ERROR = "parse_error"
    EMPTY_RESPONSE = "empty_response"
    NETWORK_ERROR = "network_error"
    UNKNOWN = "unknown"


class Checkpoint(str, Enum):
    """Executor log checkpoints. Every structured log entry includes one of these.
    Required by INV-10.
    """
    START = "START"
    FETCH = "FETCH"
    PARSE = "PARSE"
    COMPLETE = "COMPLETE"
    ERROR = "ERROR"


class CircuitBreakerState(str, Enum):
    """States for the LLM API circuit breaker (Section 9.3)."""
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


# ── Recovery Map (architecture §5.2) ────────────────────────────────────────
# Maps each failure type to the ordered list of valid recovery strategies.
# The Supervisor selects from this list; LLM picks which one (INV-03).
# Rule-based fallback always picks the FIRST entry.

RECOVERY_MAP: dict[FailureType, list[StrategyEnum]] = {
    FailureType.RATE_LIMIT:     [StrategyEnum.RSS_FALLBACK, StrategyEnum.API_FALLBACK, StrategyEnum.CACHED_RESPONSE],
    FailureType.TIMEOUT:        [StrategyEnum.API_FALLBACK, StrategyEnum.CACHED_RESPONSE],
    FailureType.PARSE_ERROR:    [StrategyEnum.RSS_FALLBACK, StrategyEnum.API_FALLBACK],
    FailureType.EMPTY_RESPONSE: [StrategyEnum.RSS_FALLBACK, StrategyEnum.API_FALLBACK, StrategyEnum.CACHED_RESPONSE],
    FailureType.NETWORK_ERROR:  [StrategyEnum.CACHED_RESPONSE],
    FailureType.UNKNOWN:        [StrategyEnum.RSS_FALLBACK, StrategyEnum.API_FALLBACK, StrategyEnum.CACHED_RESPONSE],
}
