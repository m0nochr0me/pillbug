"""Workspace skill discovery — SKILL.md frontmatter contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.util.skills import discover_workspace_skills


def _write_skill(workspace_root: Path, name: str, body: str) -> Path:
    skill_dir = workspace_root / "skills" / name
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(body, encoding="utf-8")
    return skill_file


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


class TestDiscoverWorkspaceSkills:
    def test_missing_skills_directory_returns_empty(self, workspace_root: Path):
        assert discover_workspace_skills(workspace_root) == []

    def test_well_formed_skill_is_discovered(self, workspace_root: Path):
        _write_skill(
            workspace_root,
            "alpha",
            "---\nname: alpha\ndescription: first skill\n---\nbody",
        )
        skills = discover_workspace_skills(workspace_root)
        assert len(skills) == 1
        assert skills[0].name == "alpha"
        assert skills[0].description == "first skill"

    def test_quoted_frontmatter_values_are_unquoted(self, workspace_root: Path):
        _write_skill(
            workspace_root,
            "quoted",
            "---\nname: \"q-skill\"\ndescription: 'has quotes'\n---\n",
        )
        skills = discover_workspace_skills(workspace_root)
        assert skills[0].name == "q-skill"
        assert skills[0].description == "has quotes"

    def test_skill_directory_without_skill_md_is_skipped(self, workspace_root: Path):
        (workspace_root / "skills" / "empty").mkdir(parents=True)
        assert discover_workspace_skills(workspace_root) == []

    def test_missing_frontmatter_field_is_skipped(self, workspace_root: Path):
        _write_skill(workspace_root, "bad", "---\nname: only-name\n---\n")
        assert discover_workspace_skills(workspace_root) == []

    def test_multiple_skills_are_sorted_case_insensitively(self, workspace_root: Path):
        _write_skill(workspace_root, "Bravo", "---\nname: bravo\ndescription: b\n---\n")
        _write_skill(workspace_root, "alpha", "---\nname: alpha\ndescription: a\n---\n")
        _write_skill(workspace_root, "charlie", "---\nname: charlie\ndescription: c\n---\n")
        skills = discover_workspace_skills(workspace_root)
        assert [skill.name for skill in skills] == ["alpha", "bravo", "charlie"]
