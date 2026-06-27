"""
app/agents/supervisor/circuit_breaker.py
----------------------------------------
Lightweight circuit breaker for LLM API calls (architecture §9.3).
Handles transitions between CLOSED, OPEN, and HALF_OPEN.
"""

from __future__ import annotations

import time
import structlog
from app.core.config import get_settings
from app.core.enums import CircuitBreakerState
from app.core.redis_client import get_redis

logger = structlog.get_logger(__name__)


class CircuitBreaker:
    """Protects LLM API calls with a sliding window state machine.
    Uses Redis for state storage to support restarts/multi-process synchronization,
    with an in-memory fallback if Redis is unavailable.
    """
    def __init__(self) -> None:
        self.settings = get_settings()
        self.threshold = self.settings.cb_failure_threshold
        self.window = self.settings.cb_window_seconds
        self.open_duration = self.settings.cb_open_duration_seconds

        # In-memory fallback state
        self._local_state = CircuitBreakerState.CLOSED
        self._local_failures: list[float] = []
        self._local_last_state_change = 0.0

    async def _get_state(self) -> tuple[CircuitBreakerState, float, list[float]]:
        """Retrieve state from Redis or local memory."""
        try:
            client = await get_redis()
            state_str = await client.get("agentos:cb:state")
            state = CircuitBreakerState(state_str) if state_str else CircuitBreakerState.CLOSED

            last_change_str = await client.get("agentos:cb:last_change")
            last_change = float(last_change_str) if last_change_str else 0.0

            failures_str = await client.get("agentos:cb:failures")
            failures = json.loads(failures_str) if failures_str else []
            return state, last_change, [float(f) for f in failures]
        except Exception:
            return self._local_state, self._local_last_state_change, self._local_failures

    async def _set_state(self, state: CircuitBreakerState, last_change: float, failures: list[float]) -> None:
        """Persist state to Redis or local memory."""
        try:
            client = await get_redis()
            await client.set("agentos:cb:state", state.value)
            await client.set("agentos:cb:last_change", str(last_change))
            import json
            await client.set("agentos:cb:failures", json.dumps(failures))

            # Update metrics
            # 0=CLOSED, 1=OPEN, 2=HALF_OPEN
            state_val = 0
            if state == CircuitBreakerState.OPEN:
                state_val = 1
            elif state == CircuitBreakerState.HALF_OPEN:
                state_val = 2
            # Publish state value to Redis for monitor metric scraping
            await client.set("agentos:cb:metric_value", str(state_val))
        except Exception:
            pass

        self._local_state = state
        self._local_last_state_change = last_change
        self._local_failures = failures

    async def allow_request(self) -> bool:
        """Check if the request should be allowed or failed fast."""
        state, last_change, failures = await self._get_state()
        now = time.time()

        if state == CircuitBreakerState.OPEN:
            if now - last_change > self.open_duration:
                # Transition to HALF_OPEN to probe
                logger.warning("circuit_breaker_transition_half_open", reason="Open duration expired")
                await self._set_state(CircuitBreakerState.HALF_OPEN, now, failures)
                return True
            else:
                logger.warning("circuit_breaker_blocked_request", state=state.value, time_remaining=int(self.open_duration - (now - last_change)))
                return False

        return True

    async def record_success(self) -> None:
        """Record a successful LLM call. Closes the circuit if HALF_OPEN."""
        state, last_change, failures = await self._get_state()
        now = time.time()

        if state == CircuitBreakerState.HALF_OPEN:
            logger.info("circuit_breaker_transition_closed", reason="Probe request succeeded")
            await self._set_state(CircuitBreakerState.CLOSED, now, [])
        elif state == CircuitBreakerState.CLOSED:
            # Clear historical failures
            if failures:
                await self._set_state(CircuitBreakerState.CLOSED, now, [])

    async def record_failure(self) -> None:
        """Record a failed LLM call. Opens the circuit if threshold is reached."""
        state, last_change, failures = await self._get_state()
        now = time.time()

        failures.append(now)
        # Filter failures outside the sliding window
        failures = [f for f in failures if now - f <= self.window]

        if state == CircuitBreakerState.HALF_OPEN:
            logger.error("circuit_breaker_transition_open", reason="Probe request failed")
            await self._set_state(CircuitBreakerState.OPEN, now, failures)
        elif state == CircuitBreakerState.CLOSED:
            if len(failures) >= self.threshold:
                logger.error("circuit_breaker_transition_open", reason=f"{len(failures)} failures in {self.window}s")
                await self._set_state(CircuitBreakerState.OPEN, now, failures)
            else:
                await self._set_state(CircuitBreakerState.CLOSED, last_change, failures)
