"""
benchmark/harness.py
--------------------
Automated benchmark harness for AgentOS Lite (architecture §2.2, §13.3).
Spawns background system daemons, executes 5 targeted benchmark tasks covering
all recovery paths, polls task statuses, and prints a full trace report.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

# Add project root to python path
sys.path.append(str(Path(__file__).parent.parent.absolute()))

# Ensure .env values are populated for testing if not set
os.environ.setdefault("AGENTOS_API_KEY", "test_api_key_123_abc_456")
os.environ.setdefault("GOOGLE_API_KEY", "dummy-key-for-rules-fallback")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/agentos.db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, init_db
from app.core.enums import TaskStatus
from app.core.models import Task, InterventionRecord

settings = get_settings()


async def check_redis_alive() -> bool:
    """Check if Redis is running."""
    import redis.asyncio as aioredis
    try:
        r = aioredis.from_url(settings.redis_url)
        await r.ping()
        await r.aclose()
        return True
    except Exception:
        return False


async def seed_cache_for_network_error_task(target_url: str) -> None:
    """Pre-seed Redis with a cached result for the network-error task.

    Task D deliberately targets an unresolvable domain so that html_scraping
    raises a ConnectError (network_error). The Supervisor then selects
    cached_response, which needs a Redis key to exist. We plant it here
    before the benchmark starts so the recovery path can succeed.
    """
    import redis.asyncio as aioredis
    import json as _json

    seeded_result = {
        "items": [
            "AgentOS Lite — cached result (pre-seeded for E2E benchmark)",
            "Demonstrates successful cached_response recovery path",
            "This data was planted by the harness before the benchmark run",
        ],
        "item_count": 3,
        "source_url": target_url,
        "strategy": "cached_response",
        "latency_ms": 0,
        "cached": True,
    }

    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    cache_key = f"agentos:result:url:{target_url}"
    await r.set(cache_key, _json.dumps(seeded_result), ex=3600)
    await r.aclose()
    print(f"   [OK] Pre-seeded Redis cache key: {cache_key}")


def fmt_time(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    return ts.strftime("%H:%M:%S.%f")[:-3]


async def run_benchmark() -> None:
    print("=" * 72)
    print("   [**] AGENTOS LITE -- 5-TASK END-TO-END BENCHMARK HARNESS [**]")
    print("=" * 72)

    # 1. Initialize DB schema
    print("\n[1/7] Initialising database schema...")
    await init_db()
    print("   [OK] Done.")

    # 2. Check Redis
    print("[2/7] Checking Redis connection...")
    if not await check_redis_alive():
        print("[FAIL] Redis is not running! Start it with: docker compose up -d redis")
        sys.exit(1)
    print("   [OK] Redis is alive.")

    # 3. Pre-seed cache for Task D (network_error → cached_response)
    print("[3/7] Pre-seeding Redis cache for network-error recovery task...")
    network_error_target = "https://thisdomaindoesnotexist.agentos.invalid"
    await seed_cache_for_network_error_task(network_error_target)

    # 4. Start background services
    print("[4/7] Spawning background services (FastAPI / Monitor / Supervisor)...")
    processes: list[tuple[str, subprocess.Popen]] = []

    # Truncate log files so we can detect fresh startup messages reliably
    Path("logs/supervisor.log").write_text("", encoding="utf-8")
    Path("logs/monitor.log").write_text("", encoding="utf-8")
    Path("logs/fastapi.log").write_text("", encoding="utf-8")

    try:
        env = {**os.environ}
        # Pass explicit Python path so subprocesses use the same interpreter
        py_exe = sys.executable

        fastapi_proc = subprocess.Popen(
            [py_exe, "-m", "uvicorn", "app.api.main:app",
             "--host", "127.0.0.1", "--port", "8000"],
            stdout=open("logs/fastapi.log", "a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            env=env,
        )
        processes.append(("FastAPI", fastapi_proc))

        monitor_proc = subprocess.Popen(
            [py_exe, "-m", "app.agents.monitor.agent"],
            stdout=open("logs/monitor.log", "a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            env=env,
        )
        processes.append(("Monitor", monitor_proc))

        supervisor_proc = subprocess.Popen(
            [py_exe, "-m", "app.agents.supervisor.agent"],
            stdout=open("logs/supervisor.log", "a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            env=env,
        )
        processes.append(("Supervisor", supervisor_proc))

        # Wait for FastAPI health (up to 15 s)
        print("   Waiting for FastAPI to bind...")
        fastapi_ready = False
        for _ in range(15):
            time.sleep(1)
            try:
                import httpx as _httpx
                r = _httpx.get("http://127.0.0.1:8000/health", timeout=2)
                if r.status_code == 200:
                    fastapi_ready = True
                    break
            except Exception:
                pass
        if fastapi_ready:
            print("   [OK] FastAPI health: 200")
        else:
            print("   [WARN] FastAPI did not respond within 15 s -- continuing")

        # Wait for Supervisor to subscribe to Redis (up to 30 s)
        # This is critical: TASK_CREATED pub/sub events are fire-and-forget;
        # submitting tasks before the Supervisor is subscribed means the events
        # are silently dropped and tasks stay PENDING forever.
        print("   Waiting for Supervisor to subscribe to Redis...")
        supervisor_ready = False
        for _ in range(30):
            time.sleep(1)
            try:
                log_content = Path("logs/supervisor.log").read_text(encoding="utf-8", errors="replace")
                if "Listening on Redis" in log_content:
                    supervisor_ready = True
                    break
            except Exception:
                pass
        if supervisor_ready:
            print("   [OK] Supervisor is subscribed to Redis channels.")
        else:
            print("   [WARN] Supervisor did not log 'Listening on Redis' within 30 s.")
            print("          Supervisor stderr may contain an import error:")
            try:
                tail = Path("logs/supervisor.log").read_text(encoding="utf-8", errors="replace")[-1000:]
                print(tail)
            except Exception:
                pass

        # 5. Load benchmark tasks
        tasks_path = Path(__file__).parent / "tasks.json"
        with open(tasks_path, "r", encoding="utf-8") as f:
            bench_tasks = json.load(f)
        print(f"\n[5/7] Loaded {len(bench_tasks)} tasks from tasks.json.")
        for i, t in enumerate(bench_tasks, 1):
            print(f"   Task {i}: {t['description']}")

        # 6. Submit tasks
        print("\n[6/7] Submitting tasks to API Gateway (POST /api/v1/tasks)...")
        task_ids: list[tuple[str, str, str]] = []  # (task_id, description, expected_path)
        headers = {"Authorization": f"Bearer {settings.agentos_api_key}"}

        async with httpx.AsyncClient() as client:
            for task in bench_tasks:
                payload: dict[str, Any] = {
                    "task_type": task["task_type"],
                    "target": task["target"],
                    "max_items": task.get("max_items", 10),
                }
                if "rss_feed_url" in task:
                    payload["rss_feed_url"] = task["rss_feed_url"]
                if "api_endpoint" in task:
                    payload["api_endpoint"] = task["api_endpoint"]

                try:
                    resp = await client.post(
                        "http://127.0.0.1:8000/api/v1/tasks",
                        json=payload,
                        headers=headers,
                        timeout=8.0,
                    )
                    if resp.status_code == 202:
                        tid = resp.json()["task_id"]
                        task_ids.append((tid, task["description"], task.get("expected_path", "")))
                        print(f"   [OK] {tid}  <-  {task['description'][:60]}")
                    else:
                        print(f"   [FAIL] Submission failed ({resp.status_code}): {task['description'][:60]}")
                except Exception as e:
                    print(f"   [FAIL] Submission error: {e}")

                await asyncio.sleep(0.3)

        # 7. Poll for completion
        print(f"\n[7/7] Polling {len(task_ids)} tasks for terminal state (max 120 s)...")
        start_poll = time.time()
        tids_only = [t[0] for t in task_ids]

        while time.time() - start_poll < 120:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(Task)
                    .options(selectinload(Task.interventions))
                    .where(Task.task_id.in_(tids_only))
                )
                db_tasks = result.scalars().all()

            statuses = [t.status for t in db_tasks]
            n_active = sum(1 for s in statuses if s in ("PENDING", "RUNNING", "RECOVERING"))
            n_complete = sum(1 for s in statuses if s == "COMPLETE")
            n_failed = sum(1 for s in statuses if s in ("FAILED_PERMANENT", "FAILED_MAX_RETRIES"))
            elapsed = int(time.time() - start_poll)
            print(f"   [{elapsed:>3}s] COMPLETE={n_complete}  FAILED={n_failed}  ACTIVE={n_active}")

            if n_active == 0:
                break
            await asyncio.sleep(4)
        else:
            print("   [WARN] Timeout reached (120 s). Some tasks still active.")

        # ── Final Report ──────────────────────────────────────────────────────
        await compile_report(task_ids, tids_only)

    finally:
        print("\nStopping background services...")
        for name, proc in processes:
            try:
                proc.terminate()
                proc.wait(timeout=3)
                print(f"   [OK] {name} stopped.")
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


async def compile_report(
    task_ids: list[tuple[str, str, str]],
    tids_only: list[str],
) -> None:
    """Read final DB state and print a per-task trace plus summary statistics."""

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task)
            .options(selectinload(Task.interventions))
            .where(Task.task_id.in_(tids_only))
        )
        db_tasks_raw = result.scalars().all()

    # Map task_id → Task
    db_map = {t.task_id: t for t in db_tasks_raw}
    # Preserve submission order
    ordered_tasks = [db_map[tid] for tid in tids_only if tid in db_map]

    print()
    print("=" * 72)
    print("   END-TO-END BENCHMARK REPORT")
    print("=" * 72)

    for idx, (tid, desc, expected) in enumerate(task_ids, 1):
        task = db_map.get(tid)
        if not task:
            print(f"\nTask {idx}: {desc}")
            print("  [WARN]  NOT FOUND IN DATABASE")
            continue

        status_icon = "[PASS]" if task.status == "COMPLETE" else "[FAIL]"
        print(f"\nTask {idx}: {desc}")
        print(f"  Expected path : {expected}")
        print(f"  Final status  : {status_icon} {task.status}")
        print(f"  Attempt count : {task.attempt_count}")
        print(f"  Created at    : {fmt_time(task.created_at)}")
        print(f"  Updated at    : {fmt_time(task.updated_at)}")

        if task.interventions:
            sorted_ints = sorted(task.interventions, key=lambda i: i.attempt_number)
            print(f"  Interventions ({len(sorted_ints)}):")
            for rec in sorted_ints:
                print(
                    f"    Attempt #{rec.attempt_number:>2}  "
                    f"{rec.strategy_before or '?':>15} → {rec.strategy_after or '?':<15}  "
                    f"failure={rec.failure_type:<15}  "
                    f"confidence={rec.rca_confidence:.2f}  "
                    f"action='{rec.supervisor_action}'"
                )
        else:
            print("  Interventions : none (first-attempt success)")

        if task.status == "COMPLETE" and task.result:
            r = task.result
            print(f"  Result        : strategy={r.get('strategy','?')}  "
                  f"items={r.get('item_count',0)}  "
                  f"source={r.get('source_url','?')[:60]}")

    # ── Summary statistics ────────────────────────────────────────────────────
    total = len(ordered_tasks)
    successful = sum(1 for t in ordered_tasks if t.status == TaskStatus.COMPLETE.value)
    failed = total - successful
    completion_rate = (successful / total) if total > 0 else 0.0

    recovery_times: list[float] = []
    total_interventions = 0
    for t in ordered_tasks:
        total_interventions += len(t.interventions)
        if t.interventions and t.status == TaskStatus.COMPLETE.value:
            si = sorted(t.interventions, key=lambda i: i.created_at)
            if si and t.updated_at:
                rt = (t.updated_at - si[0].created_at).total_seconds()
                recovery_times.append(max(0.1, rt))

    mean_recovery = (sum(recovery_times) / len(recovery_times)) if recovery_times else 0.0

    print()
    print("=" * 72)
    print("   SUMMARY")
    print("=" * 72)
    print(f"  Total tasks           : {total}")
    print(f"  COMPLETE              : {successful}")
    print(f"  FAILED                : {failed}")
    print(f"  Completion rate       : {completion_rate * 100:.1f}%")
    print(f"  Total interventions   : {total_interventions}")
    print(f"  Tasks with recovery   : {len(recovery_times)}")
    print(f"  Mean recovery time    : {mean_recovery:.2f} s")

    print()
    if completion_rate >= 0.80:
        print(f"  [PASS] completion rate {completion_rate*100:.0f}% >= 80%")
    else:
        print(f"  [FAIL] completion rate {completion_rate*100:.0f}% < 80%")

    if mean_recovery <= 30.0 or not recovery_times:
        print(f"  [PASS] mean recovery time {mean_recovery:.2f} s <= 30 s")
    else:
        print(f"  [FAIL] mean recovery time {mean_recovery:.2f} s > 30 s")
    print("=" * 72)

    # Save JSON report
    report_data = {
        "timestamp": datetime.utcnow().isoformat(),
        "total_tasks": total,
        "successful_tasks": successful,
        "failed_tasks": failed,
        "completion_rate": completion_rate,
        "total_interventions": total_interventions,
        "mean_recovery_time_seconds": mean_recovery,
        "tasks": [
            {
                "task_id": t.task_id,
                "status": t.status,
                "attempts": t.attempt_count,
                "interventions": [
                    {
                        "attempt": rec.attempt_number,
                        "strategy_before": rec.strategy_before,
                        "strategy_after": rec.strategy_after,
                        "failure_type": rec.failure_type,
                        "rca_confidence": rec.rca_confidence,
                        "action": rec.supervisor_action,
                    }
                    for rec in sorted(t.interventions, key=lambda i: i.attempt_number)
                ],
                "result_strategy": t.result.get("strategy") if t.result else None,
            }
            for t in ordered_tasks
        ],
    }

    report_path = Path(__file__).parent / "report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)
    print(f"\n  Report JSON → {report_path}")


if __name__ == "__main__":
    asyncio.run(run_benchmark())
