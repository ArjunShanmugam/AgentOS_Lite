"""
tests/integration/test_smoke.py
-------------------------------
Integration smoke tests for task submission and status retrieval.
"""

from __future__ import annotations

import os
import pytest
from fastapi.testclient import TestClient

# Configure env vars before importing app
os.environ["AGENTOS_API_KEY"] = "test_api_key_123_abc_456"
os.environ["GOOGLE_API_KEY"] = "dummy-key"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./data/agentos_test.db"

from app.api.main import app
from app.core.enums import TaskStatus


def test_submit_and_get_task_flow():
    with TestClient(app) as client:
        # Submit task without auth -> Expect 403 or 401
        payload = {
            "task_type": "web_scrape",
            "target": "https://example.com",
            "max_items": 10
        }
        resp = client.post("/api/v1/tasks", json=payload)
        assert resp.status_code in (401, 403)

        # Submit task with correct auth
        headers = {"Authorization": "Bearer test_api_key_123_abc_456"}
        resp = client.post("/api/v1/tasks", json=payload, headers=headers)
        assert resp.status_code == 202
        data = resp.json()
        assert "task_id" in data
        assert data["status"] == TaskStatus.PENDING.value

        # Get status of the submitted task
        task_id = data["task_id"]
        get_resp = client.get(f"/api/v1/tasks/{task_id}", headers=headers)
        assert get_resp.status_code == 200
        status_data = get_resp.json()
        assert status_data["task_id"] == task_id
        assert status_data["status"] in (TaskStatus.PENDING.value, TaskStatus.RUNNING.value)
        assert status_data["attempt_count"] == 0

