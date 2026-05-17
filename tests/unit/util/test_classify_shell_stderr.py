"""Unit tests for app.util.text.classify_shell_stderr (plan P2 #14)."""

from __future__ import annotations

from app.util.text import classify_shell_stderr


def test_command_not_found_variants():
    assert classify_shell_stderr("bash: foo: command not found") == "command_not_found"
    assert classify_shell_stderr("zsh: command not found: foo") == "command_not_found"
    assert classify_shell_stderr("/bin/sh: 1: foo: not found") == "command_not_found"


def test_permission_denied():
    assert classify_shell_stderr("bash: ./run.sh: Permission denied") == "permission_denied"


def test_no_such_file_or_directory():
    assert classify_shell_stderr("cat: foo.txt: No such file or directory") == "no_such_file_or_directory"


def test_unknown_returns_none():
    assert classify_shell_stderr("segmentation fault (core dumped)") is None
    assert classify_shell_stderr("") is None


def test_only_first_200_chars_inspected():
    # Pattern past the 200-char window must not classify.
    padded = "x" * 250 + " command not found"
    assert classify_shell_stderr(padded) is None
