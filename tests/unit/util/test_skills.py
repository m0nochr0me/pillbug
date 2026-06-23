"""Workspace skill discovery — SKILL.md frontmatter contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.util.skills import discover_workspace_skills, extract_skill_body


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


class TestAutoLoadFrontmatter:
    def test_auto_load_defaults_to_false(self, workspace_root: Path):
        _write_skill(workspace_root, "alpha", "---\nname: alpha\ndescription: d\n---\nbody")
        assert discover_workspace_skills(workspace_root)[0].auto_load is False

    def test_auto_load_true_is_parsed(self, workspace_root: Path):
        _write_skill(workspace_root, "alpha", "---\nname: alpha\ndescription: d\nauto_load: true\n---\nbody")
        assert discover_workspace_skills(workspace_root)[0].auto_load is True

    @pytest.mark.parametrize("value", ["true", "True", "YES", "on", "1", '"true"'])
    def test_truthy_values_enable_auto_load(self, workspace_root: Path, value: str):
        _write_skill(workspace_root, "alpha", f"---\nname: alpha\ndescription: d\nauto_load: {value}\n---\n")
        assert discover_workspace_skills(workspace_root)[0].auto_load is True

    @pytest.mark.parametrize("value", ["false", "no", "0", "off", "maybe", ""])
    def test_non_truthy_values_keep_auto_load_disabled(self, workspace_root: Path, value: str):
        _write_skill(workspace_root, "alpha", f"---\nname: alpha\ndescription: d\nauto_load: {value}\n---\n")
        assert discover_workspace_skills(workspace_root)[0].auto_load is False

    def test_auto_load_after_required_fields_is_still_read(self, workspace_root: Path):
        # Regression: the parser must scan the whole frontmatter, not stop at name+description.
        _write_skill(workspace_root, "alpha", "---\nname: alpha\ndescription: d\nauto_load: true\n---\nbody")
        assert discover_workspace_skills(workspace_root)[0].auto_load is True


class TestExtractSkillBody:
    def test_returns_content_after_frontmatter(self):
        text = "---\nname: a\ndescription: d\nauto_load: true\n---\nLine one\nLine two\n"
        assert extract_skill_body(text) == "Line one\nLine two"

    def test_no_frontmatter_returns_stripped_text(self):
        assert extract_skill_body("\njust body\n") == "just body"

    def test_empty_body_returns_empty_string(self):
        assert extract_skill_body("---\nname: a\ndescription: d\n---\n") == ""
