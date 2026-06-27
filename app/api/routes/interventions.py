"""
app/api/routes/interventions.py
--------------------------------
Intervention history endpoint (architecture §11.4, Grafana Panel 4).
Returns the full append-only log of Supervisor recovery decisions.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.middleware.auth import verify_api_key
from app.core.database import get_db
from app.core.models import InterventionRecord as InterventionModel
from app.core.schemas import InterventionRecord

router = APIRouter(prefix="/api/v1/interventions", tags=["interventions"])


@router.get(
    "",
    response_model=list[InterventionRecord],
    summary="List all intervention records",
    description="Returns the append-only history of all Supervisor recovery decisions. Used by Grafana Panel 4.",
)
async def list_interventions(
    task_id: str | None = Query(None, description="Filter by task_id"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _auth: None = Depends(verify_api_key),
) -> list[InterventionRecord]:
    stmt = select(InterventionModel).order_by(InterventionModel.created_at.desc()).limit(limit)
    if task_id:
        stmt = stmt.where(InterventionModel.task_id == task_id)

    result = await db.execute(stmt)
    records = result.scalars().all()

    return [
        InterventionRecord(
            intervention_id=r.intervention_id,
            task_id=r.task_id,
            attempt_number=r.attempt_number,
            strategy_before=r.strategy_before,
            strategy_after=r.strategy_after,
            failure_type=r.failure_type,
            rca_confidence=r.rca_confidence,
            supervisor_action=r.supervisor_action,
            created_at=r.created_at,
        )
        for r in records
    ]
