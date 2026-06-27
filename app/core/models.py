"""
app/core/models.py
------------------
SQLAlchemy 2.x ORM models for all persistent entities.
Schema matches architecture §6.1 exactly.

InterventionRecord uses __table_args__ to enforce append-only at the DB
constraint level where possible (INV-06).

All UUIDs are generated in Python to keep the schema database-agnostic
(SQLite compatibility).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    JSON,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.utcnow()


class Base(DeclarativeBase):
    pass


class Task(Base):
    """Primary task record. Status transitions are guarded in application code (INV-08)."""
    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=_now, onupdate=_now)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    interventions: Mapped[list["InterventionRecord"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    event_logs: Mapped[list["EventLog"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class InterventionRecord(Base):
    """Append-only recovery decision log (INV-06, ADR-003).
    Application code NEVER issues UPDATE or DELETE against this table.
    """
    __tablename__ = "intervention_records"

    intervention_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.task_id"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    strategy_before: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_after: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_type: Mapped[str] = mapped_column(String(32), nullable=False)
    rca_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    supervisor_action: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_now)
    # NOTE: No updated_at column — intentional. This record is immutable after INSERT.

    task: Mapped["Task"] = relationship(back_populates="interventions")


class AgentConfigVersion(Base):
    """Version history for agent YAML configs (ADR-002).
    Every config write creates a new row; filesystem is written second.
    Only one row per agent_id has is_active=True at any time.
    """
    __tablename__ = "agent_config_versions"

    config_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    config_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_now)


class EventLog(Base):
    """Structured log entries from all agents, queryable by task_id (§6.1, §11.3)."""
    __tablename__ = "event_logs"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.task_id"), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    checkpoint: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_now)

    task: Mapped["Task"] = relationship(back_populates="event_logs")
