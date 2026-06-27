"""
tests/unit/test_health_score.py
-------------------------------
Unit tests for the health score formula.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import pytest

from app.core.enums import TaskStatus
from app.core.models import Task
from app.agents.monitor.health_score import calculate_health_score


def _create_task(status: TaskStatus, duration_sec: float) -> Task:
    now = datetime.utcnow()
    return Task(
        status=status.value,
        created_at=now - timedelta(seconds=duration_sec),
        updated_at=now
    )


def test_health_score_empty():
    # If no tasks, default is 1.0 (perfect)
    assert calculate_health_score([]) == 1.0


def test_health_score_all_success_low_latency():
    tasks = [_create_task(TaskStatus.COMPLETE, 2.0) for _ in range(10)]
    score = calculate_health_score(tasks)
    # success_rate = 1.0, error_rate = 0.0, latency_score = 1.0 (p95 = 2.0s < 5s)
    # Score = 0.40 * 1.0 + 0.30 * 1.0 + 0.30 * 1.0 = 1.0
    assert score == pytest.approx(1.0)


def test_health_score_with_failures():
    # 7 successes, 3 failures, low latency (< 5s)
    tasks = (
        [_create_task(TaskStatus.COMPLETE, 2.0) for _ in range(7)] +
        [_create_task(TaskStatus.FAILED_PERMANENT, 1.0) for _ in range(3)]
    )
    score = calculate_health_score(tasks)
    # success_rate = 0.70
    # error_rate = 0.30 (so 1 - error_rate = 0.70)
    # latency_score = 1.0 (p95 of completed is 2.0s < 5s)
    # Score = 0.40 * 0.70 + 0.30 * 0.70 + 0.30 * 1.0 = 0.28 + 0.21 + 0.30 = 0.79
    assert score == pytest.approx(0.79)


def test_health_score_high_latency():
    # 10 successes, but latency is high (p95 = 16.0s >= 15s)
    tasks = [_create_task(TaskStatus.COMPLETE, 16.0) for _ in range(10)]
    score = calculate_health_score(tasks)
    # success_rate = 1.0
    # error_rate = 0.0
    # latency_score = 0.0 (p95 >= 15s)
    # Score = 0.40 * 1.0 + 0.30 * 1.0 + 0.30 * 0.0 = 0.70
    assert score == pytest.approx(0.70)
