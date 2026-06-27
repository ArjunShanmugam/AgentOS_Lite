"""
app/core/database.py
--------------------
Async SQLAlchemy engine and session factory.
Development: SQLite via aiosqlite.
Production: swap DATABASE_URL to postgresql+asyncpg (no code changes needed).

Tables are created on startup via create_all (sufficient for dev/demo).
Production migrations use Alembic.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.core.models import Base

_settings = get_settings()

engine = create_async_engine(
    _settings.database_url,
    echo=False,
    # SQLite-specific: enable WAL mode for better concurrent read performance
    connect_args={"check_same_thread": False} if "sqlite" in _settings.database_url else {},
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def init_db() -> None:
    """Create all tables if they don't exist. Called on FastAPI startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that provides a scoped async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
