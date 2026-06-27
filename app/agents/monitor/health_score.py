"""
app/agents/monitor/health_score.py
----------------------------------
Calculation of the rolling agent health score (architecture §5.4).
Formula:
  Health Score = (0.40 * success_rate) + (0.30 * (1 - error_rate)) + (0.30 * latency_score)
"""

from __future__ import annotations

import math
from typing import Sequence
from app.core.enums import TaskStatus
from app.core.models import Task


def compute_p95(values: list[float]) -> float:
    """Calculate the 95th percentile of a list of floats."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = max(0, min(len(sorted_vals) - 1, math.ceil(0.95 * len(sorted_vals)) - 1))
    return sorted_vals[idx]


def calculate_health_score(recent_tasks: Sequence[Task]) -> float:
    """Compute the rolling health score based on the last 10 tasks.

    If no tasks exist, defaults to 1.0 (perfect health).
    """
    if not recent_tasks:
        return 1.0

    total_tasks = len(recent_tasks)

    successful_tasks = sum(1 for t in recent_tasks if t.status == TaskStatus.COMPLETE.value)
    failed_tasks = sum(
        1 for t in recent_tasks
        if t.status in (TaskStatus.FAILED_PERMANENT.value, TaskStatus.FAILED_MAX_RETRIES.value)
    )

    success_rate = successful_tasks / total_tasks
    error_rate = failed_tasks / total_tasks

    # Calculate latencies in seconds for all completed tasks in the window
    latencies = []
    for t in recent_tasks:
        if t.status == TaskStatus.COMPLETE.value and t.created_at and t.updated_at:
            duration = (t.updated_at - t.created_at).total_seconds()
            # Prevent negative or extremely small durations due to clock mismatch
            latencies.append(max(0.001, duration))

    if latencies:
        p95_latency = compute_p95(latencies)
    else:
        p95_latency = 0.0

    # Latency score mapping
    if p95_latency < 5.0:
        latency_score = 1.0
    elif p95_latency < 15.0:
        latency_score = 0.5
    else:
        latency_score = 0.0

    health_score = (0.40 * success_rate) + (0.30 * (1.0 - error_rate)) + (0.30 * latency_score)

    # Invariant INV-09: health score must always be in [0.0, 1.0]
    return max(0.0, min(1.0, health_score))
