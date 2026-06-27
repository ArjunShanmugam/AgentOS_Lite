"""
app/observability/metrics.py
-----------------------------
Prometheus metrics definitions (architecture §11.1).

All metrics are registered here and exported via the /metrics endpoint.
The FastAPI app calls expose_metrics() to mount the handler.

Metric naming follows the architecture spec exactly.
"""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    make_asgi_app,
    REGISTRY,
)

# ── Metric Definitions (§11.1) ────────────────────────────────────────────────

# agentos_tasks_total — Counter, labels: status, strategy
tasks_total = Counter(
    "agentos_tasks_total",
    "Total number of tasks processed, partitioned by outcome and strategy",
    labelnames=["status", "strategy"],
)

# agentos_task_completion_rate — Gauge (rolling, updated by Monitor)
task_completion_rate = Gauge(
    "agentos_task_completion_rate",
    "Rolling task completion rate over the last 10 tasks (0.0 – 1.0)",
)

# agentos_recovery_time_seconds — Histogram, labels: failure_type
recovery_time_seconds = Histogram(
    "agentos_recovery_time_seconds",
    "Time from failure detection to Supervisor recovery action start",
    labelnames=["failure_type"],
    buckets=[2.0, 5.0, 10.0, 15.0, 30.0],
)

# agentos_agent_health_score — Gauge, labels: agent_id
agent_health_score = Gauge(
    "agentos_agent_health_score",
    "Current health score per agent (0.0 – 1.0); updated every scrape interval",
    labelnames=["agent_id"],
)

# agentos_supervisor_interventions_total — Counter, labels: strategy_before, strategy_after
supervisor_interventions_total = Counter(
    "agentos_supervisor_interventions_total",
    "Number of Supervisor recovery interventions by strategy transition",
    labelnames=["strategy_before", "strategy_after"],
)

# agentos_llm_api_latency_seconds — Histogram
llm_api_latency_seconds = Histogram(
    "agentos_llm_api_latency_seconds",
    "LLM API call latency distribution",
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)

# agentos_circuit_breaker_state — Gauge, labels: component
# Values: 0=CLOSED, 1=OPEN, 2=HALF_OPEN
circuit_breaker_state = Gauge(
    "agentos_circuit_breaker_state",
    "Current circuit breaker state per component (0=CLOSED, 1=OPEN, 2=HALF_OPEN)",
    labelnames=["component"],
)


def get_metrics_app():
    """Return a Prometheus ASGI app for mounting at /metrics."""
    return make_asgi_app()
