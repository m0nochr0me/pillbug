"""Unit tests for the per-task forbidden-actions registry (plan P2 #12)."""

from __future__ import annotations

import pytest

from app.runtime import task_runtime_state


@pytest.fixture(autouse=True)
def clean_registry():
    task_runtime_state._task_forbidden_actions.clear()
    yield
    task_runtime_state._task_forbidden_actions.clear()


def test_set_and_read_round_trip():
    task_runtime_state.set_task_forbidden_actions("task:abc", ("send_message", "execute_command"))
    assert task_runtime_state.task_forbidden_actions_for_session("task:abc") == frozenset(
        {"send_message", "execute_command"}
    )


def test_empty_actions_remove_entry():
    task_runtime_state.set_task_forbidden_actions("task:abc", ("send_message",))
    task_runtime_state.set_task_forbidden_actions("task:abc", ())
    assert task_runtime_state.task_forbidden_actions_for_session("task:abc") == frozenset()


def test_clear_removes_entry():
    task_runtime_state.set_task_forbidden_actions("task:abc", ("send_message",))
    task_runtime_state.clear_task_forbidden_actions("task:abc")
    assert task_runtime_state.task_forbidden_actions_for_session("task:abc") == frozenset()


def test_none_or_empty_session_key_returns_empty():
    assert task_runtime_state.task_forbidden_actions_for_session(None) == frozenset()
    assert task_runtime_state.task_forbidden_actions_for_session("") == frozenset()
    assert task_runtime_state.task_forbidden_actions_for_session("   ") == frozenset()


def test_unrelated_session_unaffected():
    task_runtime_state.set_task_forbidden_actions("task:abc", ("send_message",))
    assert task_runtime_state.task_forbidden_actions_for_session("task:other") == frozenset()


def test_blank_action_names_are_dropped():
    task_runtime_state.set_task_forbidden_actions("task:abc", ("send_message", "", "  "))
    assert task_runtime_state.task_forbidden_actions_for_session("task:abc") == frozenset({"send_message"})
