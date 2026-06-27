"""
app/agents/supervisor/config_writer.py
--------------------------------------
Atomic configuration writer implementing the two-phase commit protocol (architecture §6.3, §6.4, §8.5, ADR-002).
DB write happens first, followed by atomic filesystem swap. DB rolled back on FS failure.
"""

from __future__ import annotations

import os
import structlog
from pathlib import Path
import yaml
from sqlalchemy.future import select

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.core.enums import StrategyEnum
from app.core.models import AgentConfigVersion
from app.core.schemas import ExecutorConfig

logger = structlog.get_logger(__name__)
settings = get_settings()


async def write_executor_config(strategy: StrategyEnum) -> int:
    """Atomically write the Executor config YAML and record version in the DB.

    Follows the 2-phase protocol:
    1. DB insert of new config version (marked active), deactivate old version.
    2. Atomic file write (write to temp, then rename).
    3. Rollback DB on file write failure.

    Returns:
        The new config version number.
    """
    agent_id = settings.executor_agent_id
    config_path = Path(settings.executor_config_path)

    # 1. DB Phase
    async with AsyncSessionLocal() as session:
        try:
            # Get latest version
            result = await session.execute(
                select(AgentConfigVersion)
                .where(AgentConfigVersion.agent_id == agent_id)
                .order_by(AgentConfigVersion.version.desc())
                .limit(1)
            )
            latest = result.scalar_one_or_none()
            next_version = (latest.version + 1) if latest else 1

            # Deactivate previous active versions
            active_result = await session.execute(
                select(AgentConfigVersion)
                .where(AgentConfigVersion.agent_id == agent_id, AgentConfigVersion.is_active == True)
            )
            previous_actives = active_result.scalars().all()
            for pa in previous_actives:
                pa.is_active = False

            # Prepare yaml content
            config_data = {
                "agent_id": agent_id,
                "strategy": strategy.value,
                "schema_version": 1
            }
            # Pre-validate structure against Pydantic schema (INV-04)
            ExecutorConfig.model_validate(config_data)

            yaml_str = yaml.safe_dump(config_data)

            # Insert new config version
            new_config = AgentConfigVersion(
                agent_id=agent_id,
                version=next_version,
                config_yaml=yaml_str,
                is_active=True
            )
            session.add(new_config)
            await session.commit()

        except Exception as exc:
            await session.rollback()
            logger.error("config_db_write_failed", error=str(exc))
            raise

    # 2. File Write Phase
    temp_path = config_path.with_suffix(".tmp")
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        # Write to temp file
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(yaml_str)

        # Atomic swap (rename)
        os.replace(temp_path, config_path)
        logger.info("config_filesystem_write_success", version=next_version, path=str(config_path))
        return next_version

    except Exception as exc:
        logger.error("config_filesystem_write_failed", error=str(exc))
        # Rollback DB changes
        async with AsyncSessionLocal() as session:
            try:
                # Delete the failed version
                failed_res = await session.execute(
                    select(AgentConfigVersion)
                    .where(AgentConfigVersion.agent_id == agent_id, AgentConfigVersion.version == next_version)
                )
                failed_ver = failed_res.scalar_one_or_none()
                if failed_ver:
                    await session.delete(failed_ver)

                # Restore previous actives
                for pa in previous_actives:
                    restore_res = await session.execute(
                        select(AgentConfigVersion).where(AgentConfigVersion.config_id == pa.config_id)
                    )
                    to_restore = restore_res.scalar_one()
                    to_restore.is_active = True

                await session.commit()
                logger.info("config_db_rollback_success", restored_version=next_version - 1)
            except Exception as rollback_exc:
                logger.critical("config_db_rollback_failed", error=str(rollback_exc))
                await session.rollback()

        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        raise
