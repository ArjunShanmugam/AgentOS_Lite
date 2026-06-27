"""
app/observability/logging.py
-----------------------------
structlog configuration for all agents (architecture §11.2).

Every log entry emits JSON to both stdout and a rotating file.
Every entry MUST contain: timestamp, level, agent_id, task_id, event (INV-10).

PII redaction: fields matching common PII patterns are scrubbed before write.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any, MutableMapping

import structlog

from app.core.config import get_settings

# ── PII Redaction Patterns (architecture §8.4) ────────────────────────────────
_PII_PATTERNS = [
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b"), "[CARD]"),
    (re.compile(r"\b(\+\d{1,3}[- ]?)?\(?\d{3}\)?[- ]?\d{3}[- ]?\d{4}\b"), "[PHONE]"),
]


def _redact_pii(value: str) -> str:
    for pattern, replacement in _PII_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def _pii_processor(
    logger: Any,
    method: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """structlog processor that redacts PII from all string fields."""
    for key, value in list(event_dict.items()):
        if isinstance(value, str):
            event_dict[key] = _redact_pii(value)
    return event_dict


def configure_logging() -> None:
    """Call once at application startup to configure structlog + stdlib logging."""
    settings = get_settings()

    # ── Stdlib root logger → rotating file ───────────────────────────────────
    log_path = Path(settings.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=50 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    logging.basicConfig(
        level=logging.DEBUG,
        handlers=[file_handler, stdout_handler],
        format="%(message)s",  # structlog formats the full JSON line
    )

    # ── structlog pipeline ────────────────────────────────────────────────────
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            _pii_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_agent_logger(agent_id: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger pre-bound with agent_id context.
    Usage: log = get_agent_logger("executor-01"); log.info("event", task_id="...")
    """
    return structlog.get_logger().bind(agent_id=agent_id)
