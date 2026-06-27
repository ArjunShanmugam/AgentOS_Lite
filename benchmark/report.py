"""
benchmark/report.py
-------------------
Standalone CLI tool to read and display an existing benchmark report.
Produced by benchmark/harness.py::compile_report() and saved to benchmark/report.json.

Usage:
    python -m benchmark.report [--path benchmark/report.json]

Architecture reference: §2.2 Non-Functional Goals, §13.3 Benchmark.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def _bar(value: float, width: int = 30, fill: str = "█", empty: str = "░") -> str:
    """Render a simple ASCII progress bar for a [0, 1] fraction."""
    filled = round(value * width)
    return fill * filled + empty * (width - filled)


def _status_symbol(status: str) -> str:
    symbols = {
        "COMPLETE": "✅",
        "FAILED_PERMANENT": "❌",
        "FAILED_MAX_RETRIES": "🔁",
        "RECOVERING": "⚠️",
        "RUNNING": "🔄",
        "PENDING": "⏳",
    }
    return symbols.get(status, "❓")


def render_report(report_path: Path) -> int:
    """Load a benchmark JSON report and print a human-readable summary.

    Returns exit code 0 if assertions pass, 1 if any assertion fails.
    """
    if not report_path.exists():
        print(f"❌ Report file not found: {report_path}", file=sys.stderr)
        print("   Run `python -m benchmark.harness` first to generate a report.", file=sys.stderr)
        return 1

    with open(report_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    timestamp = data.get("timestamp", "unknown")
    total = data.get("total_tasks", 0)
    successful = data.get("successful_tasks", 0)
    failed = data.get("failed_tasks", 0)
    completion_rate = data.get("completion_rate", 0.0)
    total_interventions = data.get("total_interventions", 0)
    mean_recovery = data.get("mean_recovery_time_seconds", 0.0)
    tasks = data.get("tasks", [])

    print()
    print("=" * 70)
    print("   🔮  AGENTOS LITE — BENCHMARK REPORT VIEWER  🔮")
    print("=" * 70)
    print(f"  Report generated: {timestamp}")
    print()

    # ── Summary Section ──────────────────────────────────────────────────────
    print("  SUMMARY")
    print("  " + "─" * 50)
    print(f"  Total Tasks Tested       :  {total}")
    print(f"  Successful Tasks         :  {successful}")
    print(f"  Failed Tasks             :  {failed}")
    print(f"  Completion Rate          :  {completion_rate * 100:.2f}%")
    print(f"    {_bar(completion_rate)}  {completion_rate * 100:.1f}%")
    print(f"  Total Interventions      :  {total_interventions}")
    print(f"  Mean Recovery Time (s)   :  {mean_recovery:.2f}s")
    print()

    # ── Per-Task Details ─────────────────────────────────────────────────────
    if tasks:
        print("  TASK BREAKDOWN")
        print("  " + "─" * 50)
        print(f"  {'#':<4} {'Task ID':<10} {'Status':<24} {'Attempts':>8} {'Interv.':>8}  Description")
        print("  " + "─" * 50)

        for idx, task in enumerate(tasks, start=1):
            tid = task.get("task_id", "?")[:8]
            status = task.get("status", "?")
            attempts = task.get("attempts", 0)
            interventions = task.get("interventions", 0)
            payload = task.get("payload", {})
            description = payload.get("target", "")[:35]
            symbol = _status_symbol(status)

            print(
                f"  {idx:<4} {tid:<10} {symbol} {status:<22} {attempts:>8} {interventions:>8}  {description}"
            )

        print()

    # ── Assertions ──────────────────────────────────────────────────────────
    print("  PASS/FAIL ASSERTIONS")
    print("  " + "─" * 50)

    all_pass = True

    # Assertion 1: completion rate >= 90%
    if completion_rate >= 0.90:
        print("  ✅ PASS — Task completion rate >= 90% (architecture NFR §2.2)")
    else:
        print(f"  ❌ FAIL — Task completion rate {completion_rate * 100:.1f}% < 90% (architecture NFR §2.2)")
        all_pass = False

    # Assertion 2: mean recovery time <= 15s
    if mean_recovery <= 15.0 or mean_recovery == 0.0:
        print("  ✅ PASS — Mean recovery time <= 15 seconds (architecture NFR §2.2)")
    else:
        print(f"  ❌ FAIL — Mean recovery time {mean_recovery:.2f}s > 15s (architecture NFR §2.2)")
        all_pass = False

    # Assertion 3: interventions are logged
    if total_interventions >= 0:
        print(f"  ✅ INFO — {total_interventions} Supervisor interventions recorded in audit trail")

    print()
    print("=" * 70)

    if all_pass:
        print("  🎉 ALL ASSERTIONS PASSED — System meets benchmark targets!")
    else:
        print("  ⚠️  SOME ASSERTIONS FAILED — Review failures above.")
    print("=" * 70)
    print()

    return 0 if all_pass else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AgentOS Lite — Benchmark Report Viewer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example:\n  python -m benchmark.report\n  python -m benchmark.report --path benchmark/report.json",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=Path(__file__).parent / "report.json",
        help="Path to the benchmark report JSON file (default: benchmark/report.json)",
    )
    args = parser.parse_args()
    sys.exit(render_report(args.path))


if __name__ == "__main__":
    main()
