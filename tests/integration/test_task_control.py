"""Operator dashboard task CRUD endpoints (plan §3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import mcp as mcp_mod
from app.core.config import settings
from app.runtime.scheduler import task_scheduler


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path):
    return settings


@pytest.fixture
def dashboard_token(workspace_settings, monkeypatch):
    token = "test-dashboard-token-32characters"
    monkeypatch.setattr(settings, "DASHBOARD_BEARER_TOKEN", SecretStr(token))
    return token


@pytest.fixture
def control_client(workspace_settings):
    return TestClient(mcp_mod.mcp_app)


@pytest.fixture
def auth_headers(dashboard_token):
    return {"Authorization": f"Bearer {dashboard_token}"}


class _SchedulerRecorder:
    """Tiny stub that records scheduler method calls so endpoint logic can be exercised
    without spinning up a real Docket worker.
    """

    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.delete_calls: list[str] = []
        self.create_response: dict[str, Any] = {
            "task": {
                "task_id": "generated-id",
                "name": "stub",
                "prompt": "stub",
                "schedule": {"kind": "delayed", "delay_seconds": 60, "repeat": False},
                "enabled": True,
                "revision": 1,
            }
        }
        self.update_response: dict[str, Any] = {
            "task": {
                "task_id": "existing-id",
                "name": "updated",
                "prompt": "updated prompt",
                "schedule": {"kind": "cron", "expression": "*/5 * * * *", "timezone": "UTC"},
                "enabled": True,
                "revision": 2,
            }
        }
        self.update_error: Exception | None = None
        self.delete_error: Exception | None = None

    async def create_task(self, **kwargs: Any) -> dict[str, Any]:
        self.create_calls.append(kwargs)
        return self.create_response

    async def update_task(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        self.update_calls.append({"task_id": task_id, **kwargs})
        if self.update_error is not None:
            raise self.update_error
        return self.update_response

    async def delete_task(self, task_id: str) -> dict[str, Any]:
        self.delete_calls.append(task_id)
        if self.delete_error is not None:
            raise self.delete_error
        return {"task_id": task_id, "deleted": True}


@pytest.fixture
def stub_scheduler(monkeypatch):
    recorder = _SchedulerRecorder()
    monkeypatch.setattr(task_scheduler, "create_task", recorder.create_task)
    monkeypatch.setattr(task_scheduler, "update_task", recorder.update_task)
    monkeypatch.setattr(task_scheduler, "delete_task", recorder.delete_task)
    return recorder


class TestCreateTaskEndpoint:
    def test_without_bearer_returns_401(self, control_client, dashboard_token, stub_scheduler):
        response = control_client.post(
            "/control/tasks",
            json={
                "name": "nightly",
                "prompt": "do the thing",
                "schedule_type": "cron",
                "cron_expression": "0 0 * * *",
            },
        )
        assert response.status_code == 401
        assert stub_scheduler.create_calls == []

    def test_create_cron_task(self, control_client, auth_headers, stub_scheduler):
        response = control_client.post(
            "/control/tasks",
            json={
                "name": "nightly",
                "prompt": "do the thing",
                "schedule_type": "cron",
                "cron_expression": "0 0 * * *",
                "timezone_name": "America/New_York",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["action"] == "task.create"
        assert body["details"]["task"]["task_id"] == "generated-id"

        assert len(stub_scheduler.create_calls) == 1
        call = stub_scheduler.create_calls[0]
        assert call["name"] == "nightly"
        assert call["prompt"] == "do the thing"
        assert call["schedule_type"] == "cron"
        assert call["cron_expression"] == "0 0 * * *"
        assert call["timezone_name"] == "America/New_York"

    def test_create_delayed_task_drops_cron_expression(self, control_client, auth_headers, stub_scheduler):
        response = control_client.post(
            "/control/tasks",
            json={
                "name": "delayed",
                "prompt": "fire once",
                "schedule_type": "delayed",
                "delay_seconds": 120,
                "cron_expression": "ignored",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        call = stub_scheduler.create_calls[0]
        assert call["schedule_type"] == "delayed"
        assert call["delay_seconds"] == 120
        # validator nulls out cron_expression for delayed schedules
        assert call["cron_expression"] is None

    def test_create_cron_without_expression_returns_422(self, control_client, auth_headers, stub_scheduler):
        response = control_client.post(
            "/control/tasks",
            json={
                "name": "nightly",
                "prompt": "do the thing",
                "schedule_type": "cron",
            },
            headers=auth_headers,
        )
        assert response.status_code == 422
        assert stub_scheduler.create_calls == []

    def test_create_delayed_without_delay_returns_422(self, control_client, auth_headers, stub_scheduler):
        response = control_client.post(
            "/control/tasks",
            json={
                "name": "delayed",
                "prompt": "fire once",
                "schedule_type": "delayed",
            },
            headers=auth_headers,
        )
        assert response.status_code == 422
        assert stub_scheduler.create_calls == []

    def test_create_blank_name_returns_422(self, control_client, auth_headers, stub_scheduler):
        response = control_client.post(
            "/control/tasks",
            json={
                "name": "   ",
                "prompt": "do the thing",
                "schedule_type": "delayed",
                "delay_seconds": 60,
            },
            headers=auth_headers,
        )
        assert response.status_code == 422

    def test_scheduler_value_error_returns_400(self, control_client, auth_headers, monkeypatch, stub_scheduler):
        async def _raise(**_: Any) -> dict[str, Any]:
            raise ValueError("Invalid cron expression: foo")

        monkeypatch.setattr(task_scheduler, "create_task", _raise)
        response = control_client.post(
            "/control/tasks",
            json={
                "name": "nightly",
                "prompt": "do the thing",
                "schedule_type": "cron",
                "cron_expression": "foo",
            },
            headers=auth_headers,
        )
        assert response.status_code == 400
        assert "Invalid cron expression" in response.json()["detail"]


class TestUpdateTaskEndpoint:
    def test_without_bearer_returns_401(self, control_client, dashboard_token, stub_scheduler):
        response = control_client.patch("/control/tasks/existing-id", json={"enabled": False})
        assert response.status_code == 401
        assert stub_scheduler.update_calls == []

    def test_partial_update(self, control_client, auth_headers, stub_scheduler):
        response = control_client.patch(
            "/control/tasks/existing-id",
            json={"name": "renamed", "enabled": False},
            headers=auth_headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["action"] == "task.update"
        assert body["details"]["task"]["revision"] == 2

        assert len(stub_scheduler.update_calls) == 1
        call = stub_scheduler.update_calls[0]
        assert call["task_id"] == "existing-id"
        assert call["name"] == "renamed"
        assert call["enabled"] is False
        # Untouched fields default to None
        assert call["prompt"] is None
        assert call["delay_seconds"] is None

    def test_unknown_task_returns_404(self, control_client, auth_headers, stub_scheduler):
        stub_scheduler.update_error = ValueError("Task not found: missing")
        response = control_client.patch(
            "/control/tasks/missing",
            json={"enabled": True},
            headers=auth_headers,
        )
        assert response.status_code == 404

    def test_invalid_schedule_returns_400(self, control_client, auth_headers, stub_scheduler):
        stub_scheduler.update_error = ValueError("Invalid cron expression: garbage")
        response = control_client.patch(
            "/control/tasks/existing-id",
            json={"schedule_type": "cron", "cron_expression": "garbage"},
            headers=auth_headers,
        )
        assert response.status_code == 400

    def test_goal_and_clear_goal_mutually_exclusive(self, control_client, auth_headers, stub_scheduler):
        response = control_client.patch(
            "/control/tasks/existing-id",
            json={
                "clear_goal": True,
                "goal": {"done_condition": "finished"},
            },
            headers=auth_headers,
        )
        assert response.status_code == 422
        assert stub_scheduler.update_calls == []


class TestDeleteTaskEndpoint:
    def test_without_bearer_returns_401(self, control_client, dashboard_token, stub_scheduler):
        response = control_client.delete("/control/tasks/some-id")
        assert response.status_code == 401
        assert stub_scheduler.delete_calls == []

    def test_delete_success(self, control_client, auth_headers, stub_scheduler):
        response = control_client.delete("/control/tasks/some-id", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["action"] == "task.delete"
        assert body["details"]["deleted"] is True
        assert body["details"]["task_id"] == "some-id"
        assert stub_scheduler.delete_calls == ["some-id"]

    def test_delete_unknown_returns_404(self, control_client, auth_headers, stub_scheduler):
        stub_scheduler.delete_error = ValueError("Task not found: missing")
        response = control_client.delete("/control/tasks/missing", headers=auth_headers)
        assert response.status_code == 404
