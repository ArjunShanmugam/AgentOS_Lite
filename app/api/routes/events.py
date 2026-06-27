"""
app/api/routes/events.py
------------------------
Server-Sent Events (SSE) stream endpoint (architecture §7.4).
Clients connect to GET /api/v1/tasks/{task_id}/events and receive a
real-time stream of all events for that task.

No authentication required (read-only endpoint, architecture §5.1).
Events are sourced from the Redis Pub/Sub channel and filtered by task_id.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import AsyncGenerator

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from app.core.redis_client import CHANNEL_TASK_EVENTS, get_redis

router = APIRouter(prefix="/api/v1/tasks", tags=["events"])
log = structlog.get_logger().bind(component="api.events")


async def _event_generator(
    request: Request, task_id: str
) -> AsyncGenerator[dict, None]:
    """Subscribe to the Redis task events channel and yield events for this task_id.
    Stops cleanly when the client disconnects.
    """
    client: aioredis.Redis = await get_redis()
    pubsub = client.pubsub()
    await pubsub.subscribe(CHANNEL_TASK_EVENTS)

    log.info("sse_client_connected", task_id=task_id)

    try:
        # Send a connection confirmation event
        yield {
            "event": "connected",
            "data": json.dumps({
                "task_id": task_id,
                "message": "SSE stream connected",
                "timestamp": datetime.utcnow().isoformat(),
            }),
        }

        while True:
            if await request.is_disconnected():
                log.info("sse_client_disconnected", task_id=task_id)
                break

            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0
            )
            if message and message["type"] == "message":
                try:
                    payload = json.loads(message["data"])
                    # Filter: only forward events that belong to this task
                    if payload.get("task_id") == task_id:
                        yield {
                            "event": payload.get("event", "update"),
                            "data": json.dumps(payload),
                        }
                        # Stop streaming when task reaches a terminal event
                        if payload.get("event") in (
                            "TASK_COMPLETE",
                            "TASK_FAILED_PERMANENT",
                            "TASK_FAILED_MAX_RETRIES",
                        ):
                            break
                except (json.JSONDecodeError, KeyError):
                    pass  # Malformed event; skip silently

            await asyncio.sleep(0.1)

    except asyncio.CancelledError:
        log.info("sse_stream_cancelled", task_id=task_id)
    finally:
        await pubsub.unsubscribe(CHANNEL_TASK_EVENTS)
        await pubsub.aclose()


@router.get(
    "/{task_id}/events",
    summary="Stream task events (SSE)",
    description="Real-time Server-Sent Events for a task. No authentication required.",
)
async def stream_events(task_id: str, request: Request) -> EventSourceResponse:
    return EventSourceResponse(_event_generator(request, task_id))
