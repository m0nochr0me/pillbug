"""Workspace path sandboxing — the runtime's primary file-access defense."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.util.workspace import (
    display_path,
    is_hidden_path,
    resolve_path_within_root,
    truncate_text,
)


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


class TestResolvePathWithinRoot:
    def test_relative_path_resolves_under_root(self, workspace_root: Path):
        resolved = resolve_path_within_root("nested/file.txt", workspace_root)
        assert resolved == (workspace_root / "nested" / "file.txt").resolve()

    def test_dotdot_traversal_is_rejected(self, workspace_root: Path):
        with pytest.raises(ValueError, match="escapes workspace root"):
            resolve_path_within_root("../escaped.txt", workspace_root)

    def test_deep_dotdot_traversal_is_rejected(self, workspace_root: Path):
        with pytest.raises(ValueError):
            resolve_path_within_root("a/b/../../../etc/passwd", workspace_root)

    def test_absolute_path_outside_root_is_rejected(self, workspace_root: Path, tmp_path: Path):
        outside = tmp_path / "outside.txt"
        with pytest.raises(ValueError):
            resolve_path_within_root(str(outside), workspace_root)

    def test_absolute_path_inside_root_is_accepted(self, workspace_root: Path):
        inside = workspace_root / "inside.txt"
        resolved = resolve_path_within_root(str(inside), workspace_root)
        assert resolved == inside.resolve()

    def test_symlink_escape_is_rejected(self, workspace_root: Path, tmp_path: Path):
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("nope", encoding="utf-8")

        link = workspace_root / "leak"
        link.symlink_to(outside_dir)

        with pytest.raises(ValueError):
            resolve_path_within_root("leak/secret.txt", workspace_root)

    def test_empty_string_resolves_to_root(self, workspace_root: Path):
        resolved = resolve_path_within_root("", workspace_root)
        assert resolved == workspace_root.resolve()

    def test_nul_byte_in_path_is_rejected(self, workspace_root: Path):
        # PosixPath rejects NUL bytes outright; we surface that as a typing-style
        # error rather than a silent acceptance.
        with pytest.raises((ValueError, OSError)):
            resolve_path_within_root("nested\x00file.txt", workspace_root)


class TestDisplayPath:
    def test_root_is_rendered_as_dot(self, workspace_root: Path):
        assert display_path(workspace_root, workspace_root) == "."

    def test_nested_paths_are_relative(self, workspace_root: Path):
        nested = workspace_root / "a" / "b.txt"
        assert display_path(nested, workspace_root) == "a/b.txt"


class TestIsHiddenPath:
    @pytest.mark.parametrize(
        ("relative", "expected"),
        [
            (Path(".env"), True),
            (Path("nested/.secret"), True),
            (Path("visible/file.txt"), False),
            (Path(".github/workflows/test.yml"), True),
        ],
    )
    def test_dotfile_detection(self, relative: Path, expected: bool):
        assert is_hidden_path(relative) is expected


class TestTruncateText:
    def test_short_text_passes_through(self):
        truncated, was_truncated = truncate_text("hello", 100)
        assert truncated == "hello"
        assert was_truncated is False

    def test_long_text_is_truncated_with_marker(self):
        text = "x" * 200
        truncated, was_truncated = truncate_text(text, 80)
        assert was_truncated is True
        assert truncated.endswith("characters")
        assert len(truncated) <= 80
