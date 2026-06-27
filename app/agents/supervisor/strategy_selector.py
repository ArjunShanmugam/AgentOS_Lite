"""
app/agents/supervisor/strategy_selector.py
------------------------------------------
Selects recovery strategy using Gemini LLM, with rule-based fallback (architecture §5.2, §8.5, §9.3).
"""

from __future__ import annotations

import asyncio
import json
import structlog
from typing import Sequence
import google.generativeai as genai
from google.api_core.exceptions import GoogleAPIError

from app.core.config import get_settings
from app.core.enums import FailureType, StrategyEnum, RECOVERY_MAP
from app.core.models import Task, InterventionRecord
from app.core.schemas import RCAReport, SupervisorDecision
from app.agents.supervisor.circuit_breaker import CircuitBreaker

logger = structlog.get_logger(__name__)


def get_rule_based_strategy(
    failure_type: FailureType,
    already_tried: set[StrategyEnum]
) -> tuple[StrategyEnum, str]:
    """Fallback logic when LLM is unavailable: choose the first valid strategy
    from RECOVERY_MAP that has not been tried yet.
    """
    allowed = RECOVERY_MAP.get(failure_type, RECOVERY_MAP[FailureType.UNKNOWN])
    for strat in allowed:
        if strat not in already_tried:
            return strat, f"Rule-based fallback: selected {strat.value} (first untried strategy for {failure_type.value})"

    # If all allowed are tried, default to the last allowed or cached_response
    if allowed:
        return allowed[-1], f"Rule-based fallback: all options exhausted, repeating last allowed strategy ({allowed[-1].value})"
    return StrategyEnum.CACHED_RESPONSE, "Rule-based fallback: no allowed strategies in recovery map, using cached_response"


async def select_strategy(
    task: Task,
    rca: RCAReport,
    cb: CircuitBreaker,
    interventions: Sequence[InterventionRecord]
) -> tuple[StrategyEnum, str]:
    """Select the next recovery strategy using Gemini LLM, or fall back to rule-based selection.

    Enforces the four-step LLM output validation gate (INV-03, INV-04, §8.5).
    """
    already_tried = {StrategyEnum(i.strategy_after) for i in interventions if i.strategy_after}
    # Add initial strategy if tracked
    if task.payload:
        # If task was created and ran a strategy
        pass

    failure_type = rca.failure_type
    allowed_list = RECOVERY_MAP.get(failure_type, RECOVERY_MAP[FailureType.UNKNOWN])
    # Filter out already tried strategies for prompt constraint, but keep at least one
    available_list = [s for s in allowed_list if s not in already_tried]
    if not available_list:
        available_list = allowed_list

    # Rule-based fallback if circuit breaker is OPEN
    if not await cb.allow_request():
        logger.warning("cb_open_rule_based_fallback", task_id=task.task_id)
        return get_rule_based_strategy(failure_type, already_tried)

    settings = get_settings()
    genai.configure(api_key=settings.google_api_key)

    prompt = f"""
You are the Supervisor Agent of AgentOS Lite.
Your goal is to select the next recovery strategy for a failed task.

--- Task Details ---
Task ID: {task.task_id}
Task Payload: {json.dumps(task.payload)}
Current Attempt: {task.attempt_count}
Tried Strategies So Far: {[s.value for s in already_tried]}

--- Monitor Failure RCA ---
Failure Type: {failure_type.value}
Evidence: {rca.evidence}
Confidence: {rca.confidence}
Agent Health Score: {rca.health_score}

--- Recovery Constraints ---
Allowed Strategies for this failure type: {[s.value for s in allowed_list]}
Preferred / Available Strategies (excluding tried): {[s.value for s in available_list]}

Select the single best strategy from the allowed list. Avoid tried strategies if possible.
You MUST respond with a JSON object conforming exactly to this schema:
{{
  "strategy": "string (must be one of the allowed strategies)",
  "rationale": "string (short rationale for selection)"
}}
"""

    # Retries loop (exponential backoff)
    attempts = settings.llm_max_retries + 1
    backoff = 1.0

    for attempt in range(1, attempts + 1):
        try:
            logger.info("llm_api_call_start", task_id=task.task_id, attempt=attempt)
            model = genai.GenerativeModel(settings.llm_model)

            # Generate content in a background thread or async wrapper
            loop = asyncio.get_running_loop()
            start_time = asyncio.get_event_loop().time()
            response = await loop.run_in_executor(
                None,
                lambda: model.generate_content(
                    prompt,
                    generation_config={"response_mime_type": "application/json"}
                )
            )
            latency = asyncio.get_event_loop().time() - start_time
            logger.info("llm_api_call_success", task_id=task.task_id, latency_seconds=round(latency, 2))

            await cb.record_success()

            # Parse and validate the response
            # 1. Parse JSON
            decision_data = json.loads(response.text.strip())

            # 2. Validate against Pydantic schema
            decision = SupervisorDecision.model_validate(decision_data)

            # 3. Check StrategyEnum
            strategy = decision.strategy

            # 4. Check against recovery map for failure type
            if strategy not in allowed_list:
                raise ValueError(f"LLM selected strategy '{strategy}' which is not allowed for failure '{failure_type.value}'")

            return strategy, decision.rationale

        except (GoogleAPIError, asyncio.TimeoutError) as exc:
            logger.warning("llm_api_call_exception", task_id=task.task_id, attempt=attempt, error=str(exc))
            await cb.record_failure()
            if attempt < attempts:
                await asyncio.sleep(backoff)
                backoff *= 2.0
        except Exception as exc:
            # Pydantic validation error, JSON decode error, or custom ValueError
            logger.error("llm_validation_failed", task_id=task.task_id, attempt=attempt, error=str(exc))
            # Treat schema validation / format errors as local, but still report failure to CB to be safe if it's API issue
            await cb.record_failure()
            break  # Don't retry parsing errors, go straight to fallback

    # Fall back to rule-based if all attempts fail
    logger.warning("llm_unavailable_rule_based_fallback", task_id=task.task_id)
    return get_rule_based_strategy(failure_type, already_tried)
