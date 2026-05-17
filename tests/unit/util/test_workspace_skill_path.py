"""Unit tests for workspace_skill_name_for_path (plan P2 #18 path predicate)."""

from __future__ import annotations

from pathlib import Path

from app.util.skills import workspace_skill_name_for_path


def test_matches_canonical_layout():
    assert workspace_skill_name_for_path(Path("/ws/skills/foo/SKILL.md")) == "foo"


def test_relative_path_matches_too():
    assert workspace_skill_name_for_path(Path("skills/bar/SKILL.md")) == "bar"


def test_rejects_when_grandparent_is_not_skills():
    assert workspace_skill_name_for_path(Path("/ws/random/foo/SKILL.md")) is None


def test_rejects_non_skill_filename():
    assert workspace_skill_name_for_path(Path("/ws/skills/foo/README.md")) is None


def test_rejects_skills_root_skill_md():
    # A `SKILL.md` placed directly in the skills/ root has empty/odd grandparent layout.
    assert workspace_skill_name_for_path(Path("/ws/skills/SKILL.md")) is None
