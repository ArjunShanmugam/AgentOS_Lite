"""
app/api/routes/tasks.py
-----------------------
Task submission and status endpoints (architecture §7.1, §7.2).

POST /api/v1/tasks  — validated, persisted, published to Redis
GET  /api/v1/tasks/{task_id} — status, interventions, health score
"""



import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status, Body
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.middleware.auth import verify_api_key
from app.api.middleware.rate_limit import limiter
from app.core.database import get_db
from app.core.enums import TaskStatus
from app.core.models import InterventionRecord, Task
from app.core.redis_client import CHANNEL_TASK_EVENTS, publish
from app.core.schemas import (
    InterventionSummary,
    TaskRequest,
    TaskResponse,
    TaskStatusResponse,
)

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])
log = structlog.get_logger().bind(component="api.tasks")


@router.post(
    "",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a new task",
    description="Validates the task payload, persists it with PENDING status, and enqueues it for execution.",
)
@limiter.limit("60/minute")
async def submit_task(
    request: Request,
    payload: TaskRequest = Body(...),
    db: AsyncSession = Depends(get_db),
    _auth: None = Depends(verify_api_key),
) -> TaskResponse:
    task_id = str(uuid.uuid4())

    # Persist to DB first (architecture §6.4: DB write before Redis publish)
    task = Task(
        task_id=task_id,
        payload=payload.model_dump(),
        status=TaskStatus.PENDING,
    )
    db.add(task)
    await db.flush()

    # Publish task event to Redis (non-blocking; acceptable to fail)
    event_payload = {
        "event": "TASK_CREATED",
        "task_id": task_id,
        "task_type": payload.task_type,
        "target": payload.target,
        "timestamp": datetime.utcnow().isoformat(),
    }
    await publish(CHANNEL_TASK_EVENTS, event_payload)

    log.info("task_submitted", task_id=task_id, task_type=payload.task_type)

    return TaskResponse(
        task_id=task_id,
        status=TaskStatus.PENDING,
        estimated_duration_s=30,
    )


@router.get(
    "/{task_id}",
    response_model=TaskStatusResponse,
    summary="Get task status",
    description="Returns current status, attempt count, health score, and full intervention history.",
)
async def get_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    _auth: None = Depends(verify_api_key),
) -> TaskStatusResponse:
    result = await db.execute(
        select(Task)
        .options(selectinload(Task.interventions))
        .where(Task.task_id == task_id)
    )
    task: Task | None = result.scalar_one_or_none()

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found",
        )

    # Build intervention summaries (append-only; sorted by attempt number)
    interventions = sorted(task.interventions, key=lambda i: i.attempt_number)
    intervention_summaries = [
        InterventionSummary(
            attempt=iv.attempt_number,
            failure_type=iv.failure_type,
            strategy_before=iv.strategy_before,
            strategy_after=iv.strategy_after,
            rca_confidence=iv.rca_confidence,
            timestamp=iv.created_at,
        )
        for iv in interventions
    ]

    # Determine current strategy from payload or latest intervention
    current_strategy = task.payload.get("strategy")
    if interventions:
        current_strategy = interventions[-1].strategy_after

    return TaskStatusResponse(
        task_id=task.task_id,
        status=TaskStatus(task.status),
        attempt_count=task.attempt_count,
        current_strategy=current_strategy,
        health_score=None,  # populated by Monitor via metrics; read from Prometheus
        result=task.result,
        interventions=intervention_summaries,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )
