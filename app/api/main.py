"""
app/api/main.py
---------------
FastAPI application factory.
- Registers all routers
- Mounts Prometheus /metrics (no auth — Prometheus convention, §7.3)
- Configures structlog on startup
- Initialises DB tables on startup
- Handles graceful Redis shutdown on shutdown
- RFC 7807 error format for all HTTP errors (§7.5)
"""

from __future__ import annotations

import contextlib

import structlog
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.routes import events, interventions, tasks
from app.core.config import get_settings
from app.core.database import init_db
from app.core.redis_client import close_redis
from app.observability.logging import configure_logging
from app.observability.metrics import get_metrics_app
from app.api.middleware.rate_limit import limiter

settings = get_settings()


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):  # noqa: ARG001
    """FastAPI lifespan context manager — replaces deprecated @on_event handlers."""
    configure_logging()
    await init_db()
    structlog.get_logger().bind(component="api.main").info(
        "agentos_started", environment=settings.environment
    )
    yield
    await close_redis()
    structlog.get_logger().bind(component="api.main").info("agentos_shutdown")


def create_app() -> FastAPI:
    log = structlog.get_logger().bind(component="api.main")

    app = FastAPI(
        title="AgentOS Lite",
        description="Self-healing AI agent infrastructure platform",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=_lifespan,
    )

    # ── Rate limiting ─────────────────────────────────────────────────────────
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    # ── Routers ───────────────────────────────────────────────────────────────
    app.include_router(tasks.router)
    app.include_router(events.router)
    app.include_router(interventions.router)

    # ── Prometheus /metrics (no auth — Prometheus scrape convention §7.3) ────
    metrics_app = get_metrics_app()
    app.mount("/metrics", metrics_app)

    # ── RFC 7807 error handler ────────────────────────────────────────────────
    @app.exception_handler(Exception)
    async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_exception", error=str(exc), path=str(request.url))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "type": "https://agentos.local/errors/internal_error",
                "title": "Internal server error",
                "status": 500,
                "detail": str(exc),
                "instance": str(request.url),
            },
        )

    # ── Health check (no auth) ────────────────────────────────────────────────
    @app.get("/health", tags=["system"])
    async def health() -> dict:
        return {"status": "ok", "version": "1.0.0"}

    return app


app = create_app()
