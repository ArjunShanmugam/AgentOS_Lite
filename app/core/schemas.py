"""
app/core/schemas.py
-------------------
Pydantic v2 schemas for all API request/response payloads and inter-agent
messages. These are the data contracts between every component.

All LLM output is validated against SupervisorDecision before being applied
(architecture §8.5, INV-04).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.enums import (
    Checkpoint,
    FailureType,
    StrategyEnum,
    TaskStatus,
)


# ── Task Submission (POST /api/v1/tasks) ─────────────────────────────────────

class TaskRequest(BaseModel):
    """Validated task payload submitted by clients (§7.1)."""
    task_type: str = Field(..., description="Type of task e.g. web_scrape")
    target: str = Field(..., description="Primary target URL or identifier")
    output_format: str = Field("summary_list")
    max_items: int = Field(10, ge=1, le=50)
    # Optional: fallback RSS/API targets for recovery strategies
    rss_feed_url: Optional[str] = Field(None)
    api_endpoint: Optional[str] = Field(None)
    api_headers: Optional[dict[str, str]] = Field(None)

    @field_validator("target")
    @classmethod
    def target_must_be_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("target must not be empty")
        return v.strip()


class TaskResponse(BaseModel):
    """202 Accepted response after task submission."""
    task_id: str
    status: TaskStatus
    estimated_duration_s: int = 30


# ── Task Status (GET /api/v1/tasks/{task_id}) ────────────────────────────────

class InterventionSummary(BaseModel):
    attempt: int
    failure_type: str
    strategy_before: str
    strategy_after: str
    rca_confidence: float
    timestamp: datetime


class TaskStatusResponse(BaseModel):
    """Full task status including intervention history (§7.2)."""
    task_id: str
    status: TaskStatus
    attempt_count: int
    current_strategy: Optional[str] = None
    health_score: Optional[float] = None
    result: Optional[Any] = None
    interventions: list[InterventionSummary] = []
    created_at: datetime
    updated_at: datetime


# ── Executor Log Event (INV-10) ──────────────────────────────────────────────

class ExecutorLogEvent(BaseModel):
    """Structured log entry emitted by the Executor at each checkpoint.
    Every field listed here is mandatory per INV-10.
    """
    task_id: str
    agent_id: str
    strategy: StrategyEnum
    checkpoint: Checkpoint
    status: str  # "OK" | "ERROR"
    error_type: Optional[FailureType] = None
    http_status: Optional[int] = None
    latency_ms: Optional[int] = None
    detail: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    @model_validator(mode="after")
    def error_requires_error_type(self) -> "ExecutorLogEvent":
        if self.checkpoint == Checkpoint.ERROR and self.error_type is None:
            raise ValueError("error_type is required when checkpoint is ERROR")
        return self


# ── RCA Report (Monitor → Supervisor via Redis) ───────────────────────────────

class RCAReport(BaseModel):
    """Structured Root Cause Analysis produced by the Monitor (§5.4).
    Published to Redis Pub/Sub channel; validated against this schema before publish.
    """
    task_id: str
    failure_type: FailureType
    evidence: str = Field(..., description="Human-readable evidence sentence")
    confidence: float = Field(..., ge=0.0, le=1.0)
    suggested_strategies: list[StrategyEnum]
    health_score: float = Field(..., ge=0.0, le=1.0)
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    @field_validator("suggested_strategies")
    @classmethod
    def strategies_must_be_nonempty(cls, v: list[StrategyEnum]) -> list[StrategyEnum]:
        if not v:
            raise ValueError("suggested_strategies must contain at least one strategy")
        return v


# ── Supervisor Decision (LLM output schema — INV-04) ─────────────────────────

class SupervisorDecision(BaseModel):
    """Schema that LLM output MUST conform to before any config is written.
    This is the final validation gate (§8.5 four-step validation).
    """
    strategy: StrategyEnum
    rationale: str = Field(..., description="One-sentence LLM justification")

    @field_validator("strategy")
    @classmethod
    def strategy_must_be_valid_enum(cls, v: StrategyEnum) -> StrategyEnum:
        # Pydantic already validates against StrategyEnum; this is an explicit guard.
        if v not in list(StrategyEnum):
            raise ValueError(f"'{v}' is not a valid StrategyEnum value")
        return v


# ── Executor YAML Config Schema ───────────────────────────────────────────────

class ExecutorConfig(BaseModel):
    """In-memory representation of the executor YAML config file.
    Config files are always parsed through this schema before use (INV-04).
    """
    agent_id: str
    strategy: StrategyEnum
    schema_version: int = 1


# ── SSE Event Payloads ────────────────────────────────────────────────────────

class SSEEvent(BaseModel):
    """Generic SSE event payload (§7.4)."""
    event: str
    task_id: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    data: Optional[dict[str, Any]] = None


# ── Intervention History (GET /api/v1/interventions) ─────────────────────────

class InterventionRecord(BaseModel):
    intervention_id: str
    task_id: str
    attempt_number: int
    strategy_before: str
    strategy_after: str
    failure_type: str
    rca_confidence: float
    supervisor_action: str
    created_at: datetime
