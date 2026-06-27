"""
tests/integration/test_api_smoke.py
-------------------------------------
Integration smoke tests for the FastAPI gateway layer.
Uses FastAPI's TestClient (synchronous WSGI wrapper) to test:
  - Authentication enforcement (auth middleware)
  - Task submission happy path (POST /api/v1/tasks)
  - Task status retrieval (GET /api/v1/tasks/{task_id})
  - Schema validation enforcement (422 on bad payload)
  - Health check endpoint (GET /health — no auth)
  - Interventions list endpoint (GET /api/v1/interventions)
  - 404 for unknown task IDs

These tests run against a real in-memory SQLite DB (no Redis needed).
Redis publish failures are handled gracefully by the app (non-blocking).
"""

from __future__ import annotations

import os
import pytest
from fastapi.testclient import TestClient

# Configure env vars BEFORE importing the app so settings are correct
os.environ.setdefault("AGENTOS_API_KEY", "test_api_key_123_abc_456")
os.environ.setdefault("GOOGLE_API_KEY", "dummy-key-for-rules-fallback")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/agentos_smoke_test.db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from app.api.main import app  # noqa: E402 — import after env vars
from app.core.enums import TaskStatus  # noqa: E402


API_KEY = os.environ["AGENTOS_API_KEY"]
AUTH_HEADERS = {"Authorization": f"Bearer {API_KEY}"}
VALID_TASK_PAYLOAD = {
    "task_type": "web_scrape",
    "target": "https://news.ycombinator.com",
    "max_items": 10,
}


@pytest.fixture(scope="module")
def client():
    """Create a TestClient that runs startup/shutdown lifecycle hooks."""
    with TestClient(app) as c:
        yield c


# ── Health check ──────────────────────────────────────────────────────────────

class TestHealthEndpoint:
    def test_health_returns_200(self, client: TestClient):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_response_structure(self, client: TestClient):
        resp = client.get("/health")
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data

    def test_health_requires_no_auth(self, client: TestClient):
        """Health check is public — no auth header needed."""
        resp = client.get("/health")
        assert resp.status_code == 200


# ── Authentication enforcement ────────────────────────────────────────────────

class TestAuthentication:
    def test_submit_task_without_auth_returns_403(self, client: TestClient):
        resp = client.post("/api/v1/tasks", json=VALID_TASK_PAYLOAD)
        assert resp.status_code in (401, 403), (
            f"Expected 401 or 403 without auth, got {resp.status_code}"
        )

    def test_submit_task_with_wrong_key_returns_403(self, client: TestClient):
        resp = client.post(
            "/api/v1/tasks",
            json=VALID_TASK_PAYLOAD,
            headers={"Authorization": "Bearer wrong_key_abc"},
        )
        assert resp.status_code in (401, 403)

    def test_get_task_without_auth_returns_403(self, client: TestClient):
        resp = client.get("/api/v1/tasks/nonexistent-id")
        assert resp.status_code in (401, 403)

    def test_interventions_without_auth_returns_403(self, client: TestClient):
        resp = client.get("/api/v1/interventions")
        assert resp.status_code in (401, 403)


# ── Task submission (POST /api/v1/tasks) ─────────────────────────────────────

class TestTaskSubmission:
    def test_valid_submission_returns_202(self, client: TestClient):
        resp = client.post("/api/v1/tasks", json=VALID_TASK_PAYLOAD, headers=AUTH_HEADERS)
        assert resp.status_code == 202

    def test_response_contains_task_id(self, client: TestClient):
        resp = client.post("/api/v1/tasks", json=VALID_TASK_PAYLOAD, headers=AUTH_HEADERS)
        data = resp.json()
        assert "task_id" in data
        assert isinstance(data["task_id"], str)
        assert len(data["task_id"]) > 0

    def test_response_status_is_pending(self, client: TestClient):
        resp = client.post("/api/v1/tasks", json=VALID_TASK_PAYLOAD, headers=AUTH_HEADERS)
        data = resp.json()
        assert data["status"] == TaskStatus.PENDING.value

    def test_response_contains_estimated_duration(self, client: TestClient):
        resp = client.post("/api/v1/tasks", json=VALID_TASK_PAYLOAD, headers=AUTH_HEADERS)
        data = resp.json()
        assert "estimated_duration_s" in data
        assert data["estimated_duration_s"] == 30

    def test_task_with_rss_feed_url_accepted(self, client: TestClient):
        payload = {**VALID_TASK_PAYLOAD, "rss_feed_url": "https://news.ycombinator.com/rss"}
        resp = client.post("/api/v1/tasks", json=payload, headers=AUTH_HEADERS)
        assert resp.status_code == 202

    def test_task_with_api_endpoint_accepted(self, client: TestClient):
        payload = {**VALID_TASK_PAYLOAD, "api_endpoint": "https://api.example.com/v1/news"}
        resp = client.post("/api/v1/tasks", json=payload, headers=AUTH_HEADERS)
        assert resp.status_code == 202


# ── Schema validation (422 responses) ────────────────────────────────────────

class TestSchemaValidation:
    def test_missing_target_returns_422(self, client: TestClient):
        resp = client.post(
            "/api/v1/tasks",
            json={"task_type": "web_scrape", "max_items": 10},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 422

    def test_empty_target_returns_422(self, client: TestClient):
        resp = client.post(
            "/api/v1/tasks",
            json={"task_type": "web_scrape", "target": "   ", "max_items": 10},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 422

    def test_max_items_over_50_returns_422(self, client: TestClient):
        resp = client.post(
            "/api/v1/tasks",
            json={"task_type": "web_scrape", "target": "https://example.com", "max_items": 100},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 422

    def test_max_items_zero_returns_422(self, client: TestClient):
        resp = client.post(
            "/api/v1/tasks",
            json={"task_type": "web_scrape", "target": "https://example.com", "max_items": 0},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 422

    def test_empty_body_returns_422(self, client: TestClient):
        resp = client.post("/api/v1/tasks", json={}, headers=AUTH_HEADERS)
        assert resp.status_code == 422


# ── Task status (GET /api/v1/tasks/{task_id}) ─────────────────────────────────

class TestTaskStatus:
    def test_submit_then_get_returns_200(self, client: TestClient):
        # Submit
        resp = client.post("/api/v1/tasks", json=VALID_TASK_PAYLOAD, headers=AUTH_HEADERS)
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        # Get status
        get_resp = client.get(f"/api/v1/tasks/{task_id}", headers=AUTH_HEADERS)
        assert get_resp.status_code == 200

    def test_task_status_response_structure(self, client: TestClient):
        resp = client.post("/api/v1/tasks", json=VALID_TASK_PAYLOAD, headers=AUTH_HEADERS)
        task_id = resp.json()["task_id"]

        get_resp = client.get(f"/api/v1/tasks/{task_id}", headers=AUTH_HEADERS)
        data = get_resp.json()

        assert data["task_id"] == task_id
        assert "status" in data
        assert "attempt_count" in data
        assert "interventions" in data
        assert isinstance(data["interventions"], list)

    def test_new_task_has_zero_attempts(self, client: TestClient):
        resp = client.post("/api/v1/tasks", json=VALID_TASK_PAYLOAD, headers=AUTH_HEADERS)
        task_id = resp.json()["task_id"]

        get_resp = client.get(f"/api/v1/tasks/{task_id}", headers=AUTH_HEADERS)
        data = get_resp.json()
        assert data["attempt_count"] == 0

    def test_nonexistent_task_returns_404(self, client: TestClient):
        resp = client.get(
            "/api/v1/tasks/00000000-0000-0000-0000-000000000000",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 404


# ── Interventions list (GET /api/v1/interventions) ───────────────────────────

class TestInterventionsList:
    def test_interventions_returns_200(self, client: TestClient):
        resp = client.get("/api/v1/interventions", headers=AUTH_HEADERS)
        assert resp.status_code == 200

    def test_interventions_returns_list(self, client: TestClient):
        resp = client.get("/api/v1/interventions", headers=AUTH_HEADERS)
        assert isinstance(resp.json(), list)

    def test_interventions_task_id_filter_accepted(self, client: TestClient):
        resp = client.get(
            "/api/v1/interventions?task_id=nonexistent-task",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_interventions_limit_param_accepted(self, client: TestClient):
        resp = client.get("/api/v1/interventions?limit=5", headers=AUTH_HEADERS)
        assert resp.status_code == 200
