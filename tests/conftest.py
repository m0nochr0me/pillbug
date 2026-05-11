"""
Shared test fixtures and environment bootstrap.

Bootstraps PB_* environment variables before any `app.*` module is imported, so the
module-level `Settings()` instance does not look at `~/.pillbug` and does not fail
its Gemini API key validation. Per-test fixtures provide an isolated workspace and
let individual tests override the live `settings` object via monkeypatch.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Bootstrap must happen before any `from app...` import lower in this file or in
# collected test modules. pytest loads this conftest before test discovery.
_TEST_SESSION_BASE = Path(tempfile.mkdtemp(prefix="pillbug-pytest-"))

os.environ.setdefault("PB_BASE_DIR", str(_TEST_SESSION_BASE))
os.environ.setdefault("PB_GEMINI_API_KEY", "test-gemini-key-not-real")
os.environ.setdefault("PB_GEMINI_BACKEND", "developer")
os.environ.setdefault("PB_RUNTIME_ID", "pillbug-test")
os.environ.setdefault("FASTMCP_LOG_ENABLED", "false")

# Suppress dotenv loading: pydantic-settings would otherwise read a developer .env.
os.environ.setdefault("PB_ENABLED_CHANNELS", "cli")

import pytest  # noqa: E402

from app.core.config import settings  # noqa: E402


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """Provide an isolated runtime layout under tmp_path.

    Mirrors what `__main__.py` sets up: workspace/, tasks/, sessions/, runtime_id.txt.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (tmp_path / "tasks").mkdir()
    (tmp_path / "sessions").mkdir()
    (tmp_path / "runtime_id.txt").write_text("pillbug-test\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_workspace: Path):
    """Point the live `settings` singleton at the per-test workspace.

    Returns the patched settings module so tests can mutate further fields if needed.
    """
    monkeypatch.setattr(settings, "BASE_DIR", tmp_workspace, raising=True)
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", tmp_workspace / "workspace", raising=True)
    monkeypatch.setattr(settings, "TASKS_DIR", tmp_workspace / "tasks", raising=True)
    monkeypatch.setattr(settings, "TASKS_STORE_PATH", tmp_workspace / "tasks" / "agent_tasks.json", raising=True)
    monkeypatch.setattr(
        settings,
        "SECURITY_PATTERNS_PATH",
        tmp_workspace / "security_patterns.json",
        raising=True,
    )
    monkeypatch.setattr(settings, "SESSIONS_DIR", tmp_workspace / "sessions", raising=True)
    return settings
