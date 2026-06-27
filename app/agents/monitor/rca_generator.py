"""
app/agents/monitor/rca_generator.py
-----------------------------------
Generation and publishing of the Root Cause Analysis (RCA) report (architecture §5.4).
"""

from __future__ import annotations

import structlog
from datetime import datetime
from typing import Any

from sqlalchemy.future import select

from app.core.database import AsyncSessionLocal
from app.core.enums import FailureType, RECOVERY_MAP
from app.core.models import Task
from app.core.schemas import RCAReport
from app.core.redis_client import publish_with_retry, CHANNEL_RCA
from app.agents.monitor.failure_classifier import classify_failure
from app.agents.monitor.health_score import calculate_health_score

logger = structlog.get_logger(__name__)


async def generate_and_publish_rca(task_id: str, error_log_event: dict[str, Any]) -> RCAReport | None:
    """Classify the failure, calculate health score, generate an RCA report,
    and publish it to Redis Pub/Sub for the Supervisor.
    """
    try:
        # 1. Classify failure and get evidence + confidence
        failure_type, confidence, evidence = classify_failure(error_log_event)

        # 2. Get suggested strategies from the recovery map
        suggested = RECOVERY_MAP.get(failure_type, RECOVERY_MAP[FailureType.UNKNOWN])

        # 3. Calculate rolling health score from DB
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Task)
                .order_by(Task.updated_at.desc())
                .limit(10)
            )
            recent_tasks = result.scalars().all()
            health_score = calculate_health_score(recent_tasks)

        # 4. Construct RCAReport
        rca = RCAReport(
            task_id=task_id,
            failure_type=failure_type,
            evidence=evidence,
            confidence=confidence,
            suggested_strategies=suggested,
            health_score=health_score,
            timestamp=datetime.utcnow()
        )

        # 5. Publish to Redis Pub/Sub
        logger.info(
            "publishing_rca_report",
            task_id=task_id,
            failure_type=failure_type.value,
            confidence=confidence,
            health_score=health_score,
        )
        # Use publish_with_retry (5 attempts at 5s intervals) per architecture §9.2
        success = await publish_with_retry(CHANNEL_RCA, rca.model_dump())
        if not success:
            logger.error("rca_publish_failed_permanently", task_id=task_id)

        return rca

    except Exception as exc:
        logger.exception("rca_generation_failed", task_id=task_id, error=str(exc))
        return None
