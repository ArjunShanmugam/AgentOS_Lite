"""
app/core/config.py
------------------
Centralised settings loaded from environment variables via pydantic-settings.
No secret ever appears in source code (architecture §8.3).
"""

from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── API Security ──────────────────────────────────────────────────────────
    agentos_api_key: str = Field(..., description="Bearer token for task submission")

    # ── LLM ───────────────────────────────────────────────────────────────────
    google_api_key: str = Field(..., description="Google AI Studio API key")
    llm_model: str = Field("gemini-2.5-flash", description="Gemini model name")
    llm_timeout_seconds: int = Field(30)
    llm_max_retries: int = Field(2)

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = Field("sqlite+aiosqlite:///./data/agentos.db")

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = Field("redis://localhost:6379/0")

    # ── Application ───────────────────────────────────────────────────────────
    environment: str = Field("development")
    log_level: str = Field("INFO")
    log_file: str = Field("logs/agentos.jsonl")

    # ── Agent ─────────────────────────────────────────────────────────────────
    executor_agent_id: str = Field("executor-01")
    max_task_attempts: int = Field(3)
    task_timeout_seconds: int = Field(10)
    executor_config_path: str = Field("configs/executor-01.yaml")

    # ── Rate Limiting ─────────────────────────────────────────────────────────
    rate_limit_per_minute: int = Field(60)

    # ── Prometheus ────────────────────────────────────────────────────────────
    prometheus_scrape_interval: int = Field(15)

    # ── Circuit Breaker ───────────────────────────────────────────────────────
    cb_failure_threshold: int = Field(3, description="Consecutive failures to open")
    cb_window_seconds: int = Field(60)
    cb_open_duration_seconds: int = Field(30)


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance. Call this everywhere instead of
    instantiating Settings() directly to avoid re-parsing .env on every call."""
    return Settings()
