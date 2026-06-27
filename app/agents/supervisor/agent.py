"""
app/agents/supervisor/agent.py
------------------------------
Supervisor Agent daemon utilizing LangGraph (architecture §5.2, §9.3, §14).
Listens for new tasks (TASK_CREATED) and RCA failure reports (CHANNEL_RCA),
runs a state graph to select a recovery strategy, writes configs, logs interventions,
and spawns/relaunches the Executor Agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime
from typing import TypedDict, Optional, Any, Sequence

from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from langgraph.graph import StateGraph, END

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.core.enums import (
    StrategyEnum,
    TaskStatus,
)
from app.core.models import Task, InterventionRecord
from app.core.schemas import RCAReport
from app.core.redis_client import get_redis, publish, CHANNEL_RCA, CHANNEL_TASK_EVENTS
from app.agents.supervisor.circuit_breaker import CircuitBreaker
from app.agents.supervisor.strategy_selector import select_strategy
from app.agents.supervisor.config_writer import write_executor_config
from app.observability.logging import configure_logging

# Configure logging for the Supervisor daemon process
import structlog
configure_logging()
logger = structlog.get_logger("agentos.supervisor")

settings = get_settings()
cb = CircuitBreaker()


# ── LangGraph State Definition ───────────────────────────────────────────────

class SupervisorState(TypedDict):
    task_id: str
    rca: RCAReport
    strategy_before: Optional[str]
    strategy_after: Optional[StrategyEnum]
    rationale: Optional[str]
    attempt_count: int
    max_attempts_exceeded: bool
    config_version: Optional[int]


# ── LangGraph Nodes ──────────────────────────────────────────────────────────

async def select_strategy_node(state: SupervisorState) -> dict[str, Any]:
    """LangGraph node to determine the next strategy using LLM or rule-based fallback."""
    task_id = state["task_id"]
    rca = state["rca"]

    async with AsyncSessionLocal() as session:
        # Load task and its intervention history
        result = await session.execute(
            select(Task)
            .options(selectinload(Task.interventions))
            .where(Task.task_id == task_id)
        )
        task = result.scalar_one_or_none()
        if not task:
            logger.error("task_not_found_in_node", task_id=task_id)
            return {"max_attempts_exceeded": True}

        # Check attempts limit (INV-01)
        next_attempt = task.attempt_count + 1
        if next_attempt > settings.max_task_attempts:
            logger.warning("task_max_attempts_exceeded", task_id=task_id, attempt=task.attempt_count)
            return {"max_attempts_exceeded": True, "attempt_count": task.attempt_count}

        # Determine current active strategy (before recovery)
        strategy_before = task.payload.get("strategy")
        if task.interventions:
            sorted_interventions = sorted(task.interventions, key=lambda i: i.attempt_number)
            strategy_before = sorted_interventions[-1].strategy_after
        else:
            # Fall back to load config file strategy or default html_scraping
            try:
                from app.agents.supervisor.agent import load_executor_config_strategy
                strategy_before = await load_executor_config_strategy()
            except Exception:
                strategy_before = StrategyEnum.HTML_SCRAPING.value

        # Select next strategy using the selector
        strategy_after, rationale = await select_strategy(task, rca, cb, task.interventions)

        return {
            "strategy_before": strategy_before,
            "strategy_after": strategy_after,
            "rationale": rationale,
            "attempt_count": next_attempt,
            "max_attempts_exceeded": False
        }


async def persist_intervention_node(state: SupervisorState) -> dict[str, Any]:
    """LangGraph node to update the task status to RECOVERING and save the InterventionRecord."""
    if state.get("max_attempts_exceeded"):
        # Terminal state handling
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Task).where(Task.task_id == state["task_id"]))
            task = result.scalar_one_or_none()
            if task:
                task.status = TaskStatus.FAILED_MAX_RETRIES.value
                task.updated_at = datetime.utcnow()
                await session.commit()

                # Publish terminal failure event to SSE channel
                terminal_event = {
                    "event": "TASK_FAILED_MAX_RETRIES",
                    "task_id": state["task_id"],
                    "timestamp": datetime.utcnow().isoformat(),
                    "data": {"attempt_count": task.attempt_count}
                }
                await publish(CHANNEL_TASK_EVENTS, terminal_event)
        return {"task_id": state["task_id"]}

    task_id = state["task_id"]
    attempt_count = state["attempt_count"]
    strategy_before = state["strategy_before"]
    strategy_after = state["strategy_after"]
    rca = state["rca"]
    rationale = state["rationale"]

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Task).where(Task.task_id == task_id))
        task = result.scalar_one()

        # Update task state to RECOVERING and increment attempt count
        task.status = TaskStatus.RECOVERING.value
        task.attempt_count = attempt_count
        task.updated_at = datetime.utcnow()

        # Insert InterventionRecord (INV-06: Insert only)
        # atomic transition under local transaction
        record = InterventionRecord(
            task_id=task_id,
            attempt_number=attempt_count,
            strategy_before=strategy_before,
            strategy_after=strategy_after.value,
            failure_type=rca.failure_type.value,
            rca_confidence=rca.confidence,
            supervisor_action=rationale[:64]  # truncate to fit in schema limits
        )
        session.add(record)
        await session.commit()

        # Publish CONFIG_REWRITTEN event to task events for UI stream
        rewrite_event = {
            "event": "CONFIG_REWRITTEN",
            "task_id": task_id,
            "timestamp": datetime.utcnow().isoformat(),
            "data": {
                "strategy_before": strategy_before,
                "strategy_after": strategy_after.value,
                "attempt": attempt_count,
                "rationale": rationale
            }
        }
        await publish(CHANNEL_TASK_EVENTS, rewrite_event)

    return {"task_id": state["task_id"]}


async def write_config_node(state: SupervisorState) -> dict[str, Any]:
    """LangGraph node to write the selected strategy to the active YAML config."""
    if state.get("max_attempts_exceeded"):
        return {"task_id": state["task_id"]}

    strategy_after = state["strategy_after"]
    try:
        # Atomic config write via the writer
        config_version = await write_executor_config(strategy_after)
        return {"config_version": config_version}
    except Exception as exc:
        logger.error(f"Failed to write executor config: {exc}")
        return {"task_id": state["task_id"]}


async def relaunch_executor_node(state: SupervisorState) -> dict[str, Any]:
    """LangGraph node to spawn a background Executor process to execute the task."""
    if state.get("max_attempts_exceeded"):
        return {"task_id": state["task_id"]}

    task_id = state["task_id"]
    # Trigger Executor Agent asynchronously via subprocess (spawn)
    asyncio.create_task(spawn_executor(task_id))
    return {"task_id": state["task_id"]}


# ── LangGraph Workflow Configuration ──────────────────────────────────────────

def build_supervisor_graph() -> Any:
    workflow = StateGraph(SupervisorState)

    workflow.add_node("select_strategy", select_strategy_node)
    workflow.add_node("persist_intervention", persist_intervention_node)
    workflow.add_node("write_config", write_config_node)
    workflow.add_node("relaunch_executor", relaunch_executor_node)

    workflow.set_entry_point("select_strategy")
    workflow.add_edge("select_strategy", "persist_intervention")
    workflow.add_edge("persist_intervention", "write_config")
    workflow.add_edge("write_config", "relaunch_executor")
    workflow.add_edge("relaunch_executor", END)

    return workflow.compile()


supervisor_app = build_supervisor_graph()


# ── Subprocess and Helper Routines ────────────────────────────────────────────

async def spawn_executor(task_id: str) -> None:
    """Spawn a subprocess to execute the Executor Agent for a given task ID."""
    try:
        logger.info(f"Spawning Executor Agent for task {task_id}")
        cmd = [sys.executable, "-m", "app.agents.executor.agent", "--task-id", task_id]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        # Wait for the subprocess to complete in the background and log its output
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(
                f"Executor subprocess for task {task_id} exited with status {proc.returncode}. "
                f"stderr: {stderr.decode().strip()}"
            )
        else:
            logger.info(f"Executor subprocess for task {task_id} completed successfully.")

    except Exception as exc:
        logger.exception(f"Failed to spawn executor subprocess for task {task_id}", exc_info=exc)


async def load_executor_config_strategy() -> str:
    """Helper to read active strategy from executor config YAML on start/failover."""
    from app.agents.executor.agent import load_executor_config
    try:
        config = load_executor_config()
        return config.strategy.value
    except Exception:
        return StrategyEnum.HTML_SCRAPING.value


async def handle_task_created(task_id: str) -> None:
    """Set up the initial executor config and run the executor for a newly submitted task."""
    logger.info(f"Handling initial setup for new task {task_id}")
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Task).where(Task.task_id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            logger.error(f"Created task {task_id} not found in DB")
            return

        # Write initial strategy config (html_scraping by default)
        try:
            await write_executor_config(StrategyEnum.HTML_SCRAPING)
        except Exception as exc:
            logger.error(f"Failed to write initial executor config: {exc}")
            return

        # Increment attempt to 1 and update status to RUNNING
        task.attempt_count = 1
        task.status = TaskStatus.RUNNING.value
        task.updated_at = datetime.utcnow()
        await session.commit()

        # Relaunch/spawn executor for the first run
        asyncio.create_task(spawn_executor(task_id))


async def run_supervisor_daemon() -> None:
    """Subscribes to Redis Pub/Sub, consuming task submissions and RCA reports."""
    logger.info("Supervisor Agent daemon starting...")
    client = await get_redis()
    pubsub = client.pubsub()
    await pubsub.subscribe(CHANNEL_TASK_EVENTS, CHANNEL_RCA)

    logger.info(f"Listening on Redis channels: '{CHANNEL_TASK_EVENTS}', '{CHANNEL_RCA}'")

    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message and message["type"] == "message":
                try:
                    payload = json.loads(message["data"])
                    channel = message["channel"]

                    if channel == CHANNEL_TASK_EVENTS:
                        event_name = payload.get("event")
                        task_id = payload.get("task_id")
                        if event_name == "TASK_CREATED" and task_id:
                            # Start task lifecycle
                            asyncio.create_task(handle_task_created(task_id))

                    elif channel == CHANNEL_RCA:
                        rca = RCAReport.model_validate(payload)
                        logger.info(f"Received RCA report for task {rca.task_id}")

                        # Trigger the LangGraph workflow
                        inputs = {
                            "task_id": rca.task_id,
                            "rca": rca,
                            "strategy_before": None,
                            "strategy_after": None,
                            "rationale": None,
                            "attempt_count": 0,
                            "max_attempts_exceeded": False,
                            "config_version": None,
                        }
                        asyncio.create_task(supervisor_app.ainvoke(inputs))

                except Exception as exc:
                    logger.error(f"Error handling pubsub message: {exc}")

            await asyncio.sleep(0.05)

    except KeyboardInterrupt:
        logger.info("Supervisor Agent daemon shutting down...")
    finally:
        await pubsub.unsubscribe(CHANNEL_TASK_EVENTS, CHANNEL_RCA)
        await pubsub.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(run_supervisor_daemon())
    except KeyboardInterrupt:
        logger.info("Supervisor stopped.")
