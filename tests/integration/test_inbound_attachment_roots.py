"""Per-channel inbound attachment sub-root allowlist (plan P2 #17)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.ai import resolve_inbound_attachment_path
from app.core.config import settings


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path):
    # Ensure the default per-channel inbox directories exist for resolution.
    for sub in ("inbox/telegram", "inbox/a2a", "inbox/cli"):
        (settings.WORKSPACE_ROOT / sub).mkdir(parents=True, exist_ok=True)
    return settings


def _seed_file(relative_path: str, content: str = "x") -> Path:
    target = settings.WORKSPACE_ROOT / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def test_inbound_attachment_under_channel_root_is_resolved(workspace_settings):
    target = _seed_file("inbox/telegram/photo.jpg")
    resolved = resolve_inbound_attachment_path("inbox/telegram/photo.jpg", "telegram")
    assert resolved == target


def test_inbound_attachment_outside_channel_root_is_rejected(workspace_settings):
    _seed_file("AGENTS.md", "# agents\n")
    assert resolve_inbound_attachment_path("AGENTS.md", "telegram") is None


def test_inbound_attachment_for_unknown_channel_falls_back_to_workspace_root(workspace_settings):
    target = _seed_file("AGENTS.md", "# agents\n")
    # No entry for "ferret" in defaults or operator JSON; workspace-root rule still applies.
    resolved = resolve_inbound_attachment_path("AGENTS.md", "ferret")
    assert resolved == target


def test_inbound_attachment_path_traversal_is_rejected(workspace_settings):
    assert resolve_inbound_attachment_path("../escape.txt", "telegram") is None


def test_operator_can_override_channel_root_via_json(monkeypatch, workspace_settings):
    monkeypatch.setattr(settings, "INBOUND_ATTACHMENT_ROOTS_JSON", '{"telegram": "uploads/tg"}')
    (settings.WORKSPACE_ROOT / "inbox" / "telegram").mkdir(parents=True, exist_ok=True)
    target = _seed_file("uploads/tg/voice.ogg")

    # Default location is no longer allowed under the override.
    _seed_file("inbox/telegram/voice.ogg")
    assert resolve_inbound_attachment_path("inbox/telegram/voice.ogg", "telegram") is None
    # The new sub-root is now accepted.
    assert resolve_inbound_attachment_path("uploads/tg/voice.ogg", "telegram") == target


def test_a2a_default_root_is_inbox_a2a(workspace_settings):
    target = _seed_file("inbox/a2a/payload.json")
    assert resolve_inbound_attachment_path("inbox/a2a/payload.json", "a2a") == target
    _seed_file("inbox/telegram/payload.json")
    assert resolve_inbound_attachment_path("inbox/telegram/payload.json", "a2a") is None


def test_none_source_falls_back_to_workspace_root(workspace_settings):
    target = _seed_file("anywhere/file.txt")
    assert resolve_inbound_attachment_path("anywhere/file.txt", None) == target


def test_invalid_json_is_rejected_at_config_parse_time(monkeypatch, workspace_settings):
    monkeypatch.setattr(settings, "INBOUND_ATTACHMENT_ROOTS_JSON", '"not an object"')
    with pytest.raises(ValueError, match="must be a JSON object"):
        settings.inbound_attachment_roots()


def test_absolute_sub_root_is_rejected(monkeypatch, workspace_settings):
    monkeypatch.setattr(settings, "INBOUND_ATTACHMENT_ROOTS_JSON", '{"telegram": "/etc"}')
    with pytest.raises(ValueError, match="workspace-relative"):
        settings.inbound_attachment_roots()


def test_traversing_sub_root_is_rejected(monkeypatch, workspace_settings):
    monkeypatch.setattr(settings, "INBOUND_ATTACHMENT_ROOTS_JSON", '{"telegram": "../escape"}')
    with pytest.raises(ValueError, match="must not traverse"):
        settings.inbound_attachment_roots()
