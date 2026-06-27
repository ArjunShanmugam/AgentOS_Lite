"""
app/agents/monitor/agent.py
--------------------------
Monitor Agent daemon.
Tails the structured log file (logs/agentos.jsonl), inserts log records into the EventLog DB table,
calculates rolling health scores, classifies failures, generates and publishes RCA reports.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.core.enums import Checkpoint
from app.core.models import EventLog
from app.core.redis_client import publish, CHANNEL_TASK_EVENTS
from app.agents.monitor.rca_generator import generate_and_publish_rca
from app.observability.logging import configure_logging

# Configure logging for the Monitor agent runner itself
import structlog
configure_logging()
logger = structlog.get_logger("agentos.monitor")

settings = get_settings()

EVENT_MAP = {
    "executor_task_started": "TASK_STARTED",
    "strategy_execution_error": "FETCH_FAILED",
    "strategy_execution_complete": "TASK_COMPLETE",
    "rca_generated": "RCA_GENERATED",
    "config_rewritten": "CONFIG_REWRITTEN",
}


async def process_log_line(line: str) -> None:
    """Parse a single JSON log line, store in DB, and route events/RCA as needed."""
    try:
        data = json.loads(line.strip())
    except json.JSONDecodeError:
        return

    # Check if this log event is associated with a task
    task_id = data.get("task_id")
    if not task_id:
        return

    agent_id = data.get("agent_id", "unknown")
    event_name = data.get("event", "update")
    checkpoint = data.get("checkpoint", "update")
    status = data.get("status", "OK")

    # 1. Insert into EventLog table
    try:
        async with AsyncSessionLocal() as session:
            evt = EventLog(
                task_id=task_id,
                agent_id=agent_id,
                checkpoint=checkpoint,
                status=status,
                payload=data,
                created_at=datetime.utcnow()
            )
            session.add(evt)
            await session.commit()
    except Exception as exc:
        logger.error(f"Failed to save EventLog to DB: {exc}")

    # 2. Map to SSE event and publish to Redis CHANNEL_TASK_EVENTS
    sse_event_type = EVENT_MAP.get(event_name, event_name.upper())
    sse_payload = {
        "event": sse_event_type,
        "task_id": task_id,
        "timestamp": datetime.utcnow().isoformat(),
        "data": data
    }
    await publish(CHANNEL_TASK_EVENTS, sse_payload)

    # 3. If checkpoint is ERROR, trigger RCA generation
    if checkpoint == Checkpoint.ERROR.value:
        logger.info(f"Anomaly detected for task {task_id}. Generating RCA...")
        rca_report = await generate_and_publish_rca(task_id, data)
        if rca_report:
            # Publish RCA_GENERATED to SSE stream too
            rca_sse = {
                "event": "RCA_GENERATED",
                "task_id": task_id,
                "timestamp": datetime.utcnow().isoformat(),
                "data": rca_report.model_dump()
            }
            await publish(CHANNEL_TASK_EVENTS, rca_sse)


async def run_monitor() -> None:
    """Start tailing the structured logs file."""
    log_file_path = settings.log_file
    logger.info(f"Starting Monitor Agent, tailing logs at: {log_file_path}")

    # Ensure log file exists
    path = Path(log_file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()

    # Open and seek to end to only read new logs
    with open(path, "r", encoding="utf-8") as f:
        f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if not line:
                await asyncio.sleep(0.1)
                continue
            await process_log_line(line)


if __name__ == "__main__":
    try:
        asyncio.run(run_monitor())
    except KeyboardInterrupt:
        logger.info("Monitor Agent stopped.")
