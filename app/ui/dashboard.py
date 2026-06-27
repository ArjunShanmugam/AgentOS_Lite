"""
app/ui/dashboard.py
-------------------
Streamlit User Interface Dashboard (architecture §5.1, §11.4).
Provides real-time visualization of agent health, task completion metrics,
intervention history, and interactive task submission.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path

import httpx
import pandas as pd
import streamlit as st
import yaml

# Adjust path so we can import from app
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from app.core.config import get_settings
from app.core.enums import TaskStatus, StrategyEnum
from app.core.database import AsyncSessionLocal
from app.core.models import Task, InterventionRecord, AgentConfigVersion
from sqlalchemy import select
from app.agents.monitor.health_score import calculate_health_score

settings = get_settings()

# Page Setup
st.set_page_config(
    page_title="AgentOS Lite Dashboard",
    page_icon="🔮",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom Premium CSS (Dark Theme, Glassmorphism, Rounded Cards)
st.markdown("""
<style>
    /* Main Background & Font styling */
    @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap');
    
    html, body, [class*="css"] {
        font-family: 'Outfit', sans-serif;
    }
    
    .main {
        background-color: #0f111a;
        color: #f1f3f9;
    }
    
    /* Card Container with glassmorphism */
    .glass-card {
        background: rgba(30, 34, 51, 0.45);
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 12px;
        padding: 24px;
        margin-bottom: 20px;
        box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37);
        backdrop-filter: blur(8px);
        -webkit-backdrop-filter: blur(8px);
    }
    
    /* Stat widget adjustments */
    div[data-testid="stMetricValue"] {
        font-size: 2.2rem;
        font-weight: 700;
        color: #4f46e5;
    }
    
    /* Gradient headers */
    .gradient-text {
        background: linear-gradient(135deg, #a78bfa 0%, #818cf8 50%, #60a5fa 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        font-weight: 700;
    }
    
    /* Status Badge styling */
    .status-badge {
        padding: 4px 10px;
        border-radius: 8px;
        font-size: 0.85rem;
        font-weight: 600;
        text-transform: uppercase;
        display: inline-block;
    }
    .badge-pending { background-color: #f59e0b; color: #1e1b4b; }
    .badge-running { background-color: #3b82f6; color: #eff6ff; }
    .badge-recovering { background-color: #c084fc; color: #3b0764; }
    .badge-complete { background-color: #10b981; color: #064e3b; }
    .badge-failed { background-color: #ef4444; color: #7f1d1d; }
    
</style>
""", unsafe_allow_html=True)


# Helper to run async db calls inside streamlit sync context
def run_async(coro):
    return asyncio.run(coro)


async def get_dashboard_data():
    """Load summary stats and database rows for UI."""
    async with AsyncSessionLocal() as session:
        # Load all tasks
        tasks_res = await session.execute(
            select(Task).order_by(Task.created_at.desc()).limit(50)
        )
        tasks = tasks_res.scalars().all()

        # Load interventions
        int_res = await session.execute(
            select(InterventionRecord).order_by(InterventionRecord.created_at.desc()).limit(50)
        )
        interventions = int_res.scalars().all()

        # Calculate metrics
        recent_tasks_10 = tasks[:10]
        health_score = calculate_health_score(recent_tasks_10)

        return tasks, interventions, health_score


# Load Data
tasks, interventions, health_score = run_async(get_dashboard_data())


# ─── SIDEBAR: Task Submission & Config Viewer ───────────────────────────────
with st.sidebar:
    st.markdown('<h2 class="gradient-text">🔮 AgentOS Lite Control</h2>', unsafe_allow_html=True)
    st.markdown("---")

    # Form to submit a new task
    st.markdown("### 📥 Submit New Task")
    task_type = st.selectbox("Task Type", ["web_scrape", "rss_fallback", "api_fallback"])
    target_url = st.text_input("Target URL", value="https://news.ycombinator.com")
    max_items = st.slider("Max Items", min_value=1, max_value=50, value=10)

    # Optional Fallbacks
    rss_url = st.text_input("RSS Feed URL (Optional)")
    api_url = st.text_input("API Endpoint (Optional)")

    if st.button("🚀 Execute Task"):
        if not target_url.strip():
            st.error("Target URL cannot be empty!")
        else:
            # Send API request to FastAPI gateway
            headers = {"Authorization": f"Bearer {settings.agentos_api_key}"}
            payload = {
                "task_type": task_type,
                "target": target_url,
                "max_items": max_items,
            }
            if rss_url:
                payload["rss_feed_url"] = rss_url
            if api_url:
                payload["api_endpoint"] = api_url

            # Derive Gateway URL
            gateway_url = "http://localhost:8000/api/v1/tasks"

            try:
                with st.spinner("Submitting task to API Gateway..."):
                    resp = httpx.post(gateway_url, json=payload, headers=headers, timeout=5)
                if resp.status_code in (200, 201, 202):
                    res_json = resp.json()
                    st.success(f"Task submitted! ID: {res_json.get('task_id')}")
                    # Re-run to load fresh list
                    time.sleep(1)
                    st.rerun()
                else:
                    st.error(f"Gateway rejected request ({resp.status_code}): {resp.text}")
            except Exception as e:
                st.error(f"Could not connect to API Gateway: {e}")

    st.markdown("---")
    st.markdown("### ⚙️ Active Executor Config")
    config_path = Path(settings.executor_config_path)
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            cfg_content = f.read()
        st.code(cfg_content, language="yaml")
    else:
        st.info("No active executor configuration file found.")


# ─── MAIN CONTENT: Title & Metrics Row ────────────────────────────────────────

st.markdown('<h1 class="gradient-text" style="text-align: center; margin-bottom: 30px;">🔮 AgentOS Lite Self-Healing Monitor</h1>', unsafe_allow_html=True)

# Row 1: KPI Metrics
m1, m2, m3, m4 = st.columns(4)

with m1:
    # Health Score Gauge styling
    status_color = "🟢 Healthy"
    if health_score < 0.4:
        status_color = "🔴 Critical"
    elif health_score < 0.6:
        status_color = "🟡 Degraded"

    st.markdown(f"""
    <div class="glass-card" style="text-align: center;">
        <h5 style="margin: 0; color: #a78bfa;">Agent Health Score</h5>
        <h2 style="margin: 10px 0; font-size: 2.8rem; font-weight: bold; color: #a78bfa;">{health_score:.2f}</h2>
        <span style="font-size: 0.9rem; font-weight: bold;">{status_color}</span>
    </div>
    """, unsafe_allow_html=True)

with m2:
    total_tasks = len(tasks)
    complete_tasks = sum(1 for t in tasks if t.status == TaskStatus.COMPLETE.value)
    completion_rate = (complete_tasks / total_tasks * 100) if total_tasks > 0 else 100.0

    st.markdown(f"""
    <div class="glass-card" style="text-align: center;">
        <h5 style="margin: 0; color: #60a5fa;">Task Success Rate</h5>
        <h2 style="margin: 10px 0; font-size: 2.8rem; font-weight: bold; color: #60a5fa;">{completion_rate:.1f}%</h2>
        <span style="font-size: 0.9rem; font-weight: bold; color: #94a3b8;">Total Tasks: {total_tasks}</span>
    </div>
    """, unsafe_allow_html=True)

with m3:
    total_interventions = len(interventions)
    avg_confidence = (sum(i.rca_confidence for i in interventions) / total_interventions) if total_interventions > 0 else 0.0

    st.markdown(f"""
    <div class="glass-card" style="text-align: center;">
        <h5 style="margin: 0; color: #34d399;">Supervisor Interventions</h5>
        <h2 style="margin: 10px 0; font-size: 2.8rem; font-weight: bold; color: #34d399;">{total_interventions}</h2>
        <span style="font-size: 0.9rem; font-weight: bold; color: #94a3b8;">Avg Confidence: {avg_confidence:.2f}</span>
    </div>
    """, unsafe_allow_html=True)

with m4:
    # Average task latency for last 10 completed tasks
    latencies = []
    for t in tasks[:10]:
        if t.status == TaskStatus.COMPLETE.value and t.created_at and t.updated_at:
            latencies.append((t.updated_at - t.created_at).total_seconds())
    avg_latency = sum(latencies) / len(latencies) if latencies else 0.0

    st.markdown(f"""
    <div class="glass-card" style="text-align: center;">
        <h5 style="margin: 0; color: #facc15;">Mean Process Latency</h5>
        <h2 style="margin: 10px 0; font-size: 2.8rem; font-weight: bold; color: #facc15;">{avg_latency:.2f}s</h2>
        <span style="font-size: 0.9rem; font-weight: bold; color: #94a3b8;">Based on last 10 complete</span>
    </div>
    """, unsafe_allow_html=True)


# ─── MIDDLE CONTENT: Task Registry & Timeline Stream ──────────────────────────

c1, c2 = st.columns([3, 2])

with c1:
    st.markdown('<div class="glass-card">', unsafe_allow_html=True)
    st.markdown("### 📋 Recent Tasks Registry")

    if not tasks:
        st.info("No tasks submitted yet. Submit a task using the sidebar!")
    else:
        # Prepare task table data
        task_data = []
        for t in tasks:
            status_html = ""
            status_val = t.status
            if status_val == TaskStatus.PENDING.value:
                status_html = f'<span class="status-badge badge-pending">{status_val}</span>'
            elif status_val == TaskStatus.RUNNING.value:
                status_html = f'<span class="status-badge badge-running">{status_val}</span>'
            elif status_val == TaskStatus.RECOVERING.value:
                status_html = f'<span class="status-badge badge-recovering">{status_val}</span>'
            elif status_val == TaskStatus.COMPLETE.value:
                status_html = f'<span class="status-badge badge-complete">{status_val}</span>'
            else:
                status_html = f'<span class="status-badge badge-failed">{status_val}</span>'

            # Parse creation time
            created_str = t.created_at.strftime("%H:%M:%S") if t.created_at else ""

            task_data.append({
                "ID": t.task_id[:8],
                "Target URL": t.payload.get("target", ""),
                "Type": t.payload.get("task_type", ""),
                "Attempts": t.attempt_count,
                "Status": status_html,
                "Created At": created_str,
                "Full ID": t.task_id
            })

        df_tasks = pd.DataFrame(task_data)
        st.write(df_tasks[["ID", "Target URL", "Type", "Attempts", "Created At"]].to_html(escape=False, index=False), unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        # Task details inspection
        selected_task_short = st.selectbox("Inspect Task Details & Realtime Timeline", df_tasks["ID"].unique())
        if selected_task_short:
            full_id = df_tasks[df_tasks["ID"] == selected_task_short]["Full ID"].values[0]
            selected_task = next(t for t in tasks if t.task_id == full_id)

            st.write(f"**Task Payload:**")
            st.json(selected_task.payload)

            if selected_task.result:
                st.write(f"**Execution Result:**")
                st.json(selected_task.result)
    st.markdown('</div>', unsafe_allow_html=True)


with c2:
    st.markdown('<div class="glass-card">', unsafe_allow_html=True)
    st.markdown("### 📜 Realtime Event Timeline Replay")

    if 'selected_task_short' in locals() and selected_task_short:
        # Fetch event logs from DB for the selected task
        async def get_events(tid: str):
            async with AsyncSessionLocal() as session:
                from app.core.models import EventLog
                res = await session.execute(
                    select(EventLog).where(EventLog.task_id == tid).order_by(EventLog.created_at.asc())
                )
                return res.scalars().all()

        evts = run_async(get_events(full_id))

        if not evts:
            st.info("No logs emitted for this task yet.")
        else:
            for ev in evts:
                payload = ev.payload
                checkpoint = ev.checkpoint
                status = ev.status
                time_str = ev.created_at.strftime("%H:%M:%S")

                icon = "ℹ️"
                color = "#60a5fa"
                if checkpoint == "START":
                    icon = "🏁"
                    color = "#38bdf8"
                elif checkpoint == "FETCH":
                    icon = "📥"
                    color = "#818cf8"
                elif checkpoint == "PARSE":
                    icon = "🧠"
                    color = "#a78bfa"
                elif checkpoint == "COMPLETE":
                    icon = "✅"
                    color = "#34d399"
                elif checkpoint == "ERROR":
                    icon = "🚨"
                    color = "#f87171"

                st.markdown(f"""
                <div style="border-left: 3px solid {color}; padding-left: 10px; margin-bottom: 12px;">
                    <span style="font-size: 0.8rem; color: #64748b;">{time_str}</span><br>
                    <strong>{icon} {checkpoint}</strong> (Status: {status})<br>
                    <span style="font-size: 0.9rem; color: #94a3b8;">{payload.get('event', '')} - {payload.get('detail', '') or payload.get('error_type', '')}</span>
                </div>
                """, unsafe_allow_html=True)

    else:
        st.info("Select a task from the list to view its real-time execution timeline.")
    st.markdown('</div>', unsafe_allow_html=True)


# ─── BOTTOM ROW: Intervention History Table ───────────────────────────────────

st.markdown('<div class="glass-card">', unsafe_allow_html=True)
st.markdown("### 🛠️ Self-Healing Recovery Intervention History (Audit Trail)")

if not interventions:
    st.info("No healing recovery interventions logged yet. The system is operating cleanly!")
else:
    int_data = []
    for iv in interventions:
        int_data.append({
            "Intervention ID": iv.intervention_id[:8],
            "Task ID": iv.task_id[:8],
            "Attempt #": iv.attempt_number,
            "Strategy Before": iv.strategy_before,
            "Strategy After": iv.strategy_after,
            "Classified Failure": iv.failure_type,
            "RCA Confidence": f"{iv.rca_confidence:.2f}",
            "Supervisor Rationale / Action": iv.supervisor_action,
            "Healing Date": iv.created_at.strftime("%Y-%m-%d %H:%M:%S")
        })
    df_int = pd.DataFrame(int_data)
    st.dataframe(df_int, use_container_width=True)
st.markdown('</div>', unsafe_allow_html=True)

# Auto-refresh mechanism (checks database every 5 seconds)
st.caption("Dashboard auto-polls SQLite database. Use Streamlit's refresh or sidebar triggers for live execution.")
time.sleep(5)
st.rerun()
