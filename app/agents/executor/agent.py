"""
app/agents/executor/agent.py
----------------------------
Executor Agent runner.
Reads active configuration from configs/executor-01.yaml,
fetches the task details from the DB, dispatches to the correct strategy,
emits checkpoints, and writes results back on success.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.future import select

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.core.enums import Checkpoint, StrategyEnum, TaskStatus, FailureType
from app.core.models import Task
from app.core.schemas import ExecutorConfig
from app.core.redis_client import cache_result
from app.observability.logging import configure_logging, get_agent_logger

# Import strategies
from app.agents.executor.strategies import (
    html_scraping,
    rss_fallback,
    api_fallback,
    cached_response,
)
from app.agents.executor.strategies.html_scraping import StrategyError

configure_logging()
settings = get_settings()
logger = get_agent_logger(settings.executor_agent_id)


def load_executor_config() -> ExecutorConfig:
    """Read and validate active YAML config from the filesystem."""
    config_path = Path(settings.executor_config_path)
    if not config_path.exists():
        # Write default if not found
        default_config = {
            "agent_id": settings.executor_agent_id,
            "strategy": StrategyEnum.HTML_SCRAPING.value,
            "schema_version": 1,
        }
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(default_config, f)

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return ExecutorConfig.model_validate(data)


async def execute_task(task_id: str) -> None:
    """Execute the task specified by task_id using the currently active strategy."""
    logger.info("executor_task_started", task_id=task_id)

    # 1. Load active config
    try:
        config = load_executor_config()
        strategy = config.strategy
    except Exception as exc:
        logger.error(
            "executor_config_load_failed",
            task_id=task_id,
            error=str(exc),
        )
        return

    # 2. Fetch task details from DB
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Task).where(Task.task_id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            logger.error("task_not_found", task_id=task_id)
            return

        # Double check status (INV-08: COMPLETE or FAILED_PERMANENT cannot transition back)
        if task.status in (TaskStatus.COMPLETE.value, TaskStatus.FAILED_PERMANENT.value, TaskStatus.FAILED_MAX_RETRIES.value):
            logger.warning(
                "task_already_terminal",
                task_id=task_id,
                status=task.status,
            )
            return

        # Update status to RUNNING if not already
        if task.status != TaskStatus.RUNNING.value:
            task.status = TaskStatus.RUNNING.value
            await session.commit()

        task_payload = task.payload.copy()
        # Inject task_id and target
        task_payload["task_id"] = task_id

    # 3. Dispatch to strategy
    logger.info(
        "strategy_execution_start",
        task_id=task_id,
        strategy=strategy,
        checkpoint=Checkpoint.START.value,
        status="OK",
    )

    start_time = time.monotonic()
    try:
        # Checkpoints FETCH and PARSE are embedded or represented around execution
        logger.info(
            "strategy_execution_fetch",
            task_id=task_id,
            strategy=strategy,
            checkpoint=Checkpoint.FETCH.value,
            status="OK",
        )

        if strategy == StrategyEnum.HTML_SCRAPING:
            res = await html_scraping.execute(task_payload)
        elif strategy == StrategyEnum.RSS_FALLBACK:
            res = await rss_fallback.execute(task_payload)
        elif strategy == StrategyEnum.API_FALLBACK:
            res = await api_fallback.execute(task_payload)
        elif strategy == StrategyEnum.CACHED_RESPONSE:
            res = await cached_response.execute(task_payload)
        else:
            raise ValueError(f"Unsupported strategy: {strategy}")

        logger.info(
            "strategy_execution_parse",
            task_id=task_id,
            strategy=strategy,
            checkpoint=Checkpoint.PARSE.value,
            status="OK",
        )

        latency_ms = int((time.monotonic() - start_time) * 1000)

        # Success!
        logger.info(
            "strategy_execution_complete",
            task_id=task_id,
            strategy=strategy,
            checkpoint=Checkpoint.COMPLETE.value,
            status="OK",
            latency_ms=latency_ms,
        )

        # Write result to task and mark COMPLETE (single transaction)
        async with AsyncSessionLocal() as session:
            db_result = await session.execute(select(Task).where(Task.task_id == task_id))
            db_task = db_result.scalar_one()
            db_task.status = TaskStatus.COMPLETE.value
            db_task.result = res
            db_task.updated_at = datetime.utcnow()
            await session.commit()

        # Cache last successful result in Redis (both by task_id and url)
        target_url = task_payload.get("target")
        await cache_result(task_id, res)
        if target_url:
            await cache_result(f"url:{target_url}", res)

    except StrategyError as exc:
        latency_ms = int((time.monotonic() - start_time) * 1000)
        logger.error(
            "strategy_execution_error",
            task_id=task_id,
            strategy=strategy,
            checkpoint=Checkpoint.ERROR.value,
            status="ERROR",
            error_type=exc.error_type,
            http_status=exc.http_status,
            latency_ms=latency_ms,
            detail=exc.detail,
        )
        # Note: We do NOT update the DB task status to FAILED here. The Monitor detects this log
        # and sends RCA to Supervisor to choose next strategy.
    except Exception as exc:
        latency_ms = int((time.monotonic() - start_time) * 1000)
        logger.error(
            "strategy_execution_unknown_error",
            task_id=task_id,
            strategy=strategy,
            checkpoint=Checkpoint.ERROR.value,
            status="ERROR",
            error_type=FailureType.UNKNOWN.value,
            latency_ms=latency_ms,
            detail=str(exc),
        )


def main() -> None:
    """CLI entry point for running the executor on a specific task."""
    parser = argparse.ArgumentParser(description="AgentOS Lite - Executor Agent")
    parser.add_argument("--task-id", required=True, help="UUID of the task to execute")
    args = parser.parse_args()

    configure_logging()
    asyncio.run(execute_task(args.task_id))


if __name__ == "__main__":
    main()
