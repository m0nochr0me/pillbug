"""mtime-cached read of AGENTS.md (plan P1 #8)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core import ai as ai_mod
from app.core.config import settings


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path):
    return settings


@pytest.fixture(autouse=True)
def _reset_cache():
    ai_mod._load_agents_md_cached.cache_clear()
    yield
    ai_mod._load_agents_md_cached.cache_clear()


async def test_cached_reads_skip_disk_when_mtime_unchanged(workspace_settings, monkeypatch):
    agents_path = settings.WORKSPACE_ROOT / "AGENTS.md"
    agents_path.write_text("first contents", encoding="utf-8")

    read_calls = 0
    original_read = Path.read_text

    def _counting_read(self, *args, **kwargs):
        nonlocal read_calls
        read_calls += 1
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _counting_read)

    first = await ai_mod._read_agents_md(agents_path)
    second = await ai_mod._read_agents_md(agents_path)
    third = await ai_mod._read_agents_md(agents_path)

    assert first == "first contents"
    assert second == "first contents"
    assert third == "first contents"
    assert read_calls == 1  # cache served the 2nd and 3rd reads


async def test_mtime_change_invalidates_cache(workspace_settings):
    agents_path = settings.WORKSPACE_ROOT / "AGENTS.md"
    agents_path.write_text("v1", encoding="utf-8")
    first = await ai_mod._read_agents_md(agents_path)
    assert first == "v1"

    # Edit + force a different mtime
    agents_path.write_text("v2", encoding="utf-8")
    new_mtime_ns = agents_path.stat().st_mtime_ns + 1_000_000_000
    os.utime(agents_path, ns=(new_mtime_ns, new_mtime_ns))

    second = await ai_mod._read_agents_md(agents_path)
    assert second == "v2"


async def test_missing_file_returns_empty_string(workspace_settings):
    result = await ai_mod._read_agents_md(settings.WORKSPACE_ROOT / "DOES_NOT_EXIST.md")
    assert result == ""
