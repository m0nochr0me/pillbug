"""Session correlation between MCP transports and runtime sessions."""

from __future__ import annotations

import pytest

from app.runtime import session_binding
from app.schema.todo import TodoItem, TodoListSnapshot


@pytest.fixture(autouse=True)
def reset_session_binding_state():
    """Ensure module-level dictionaries don't leak between tests."""
    session_binding._mcp_runtime_sessions.clear()
    session_binding._runtime_session_origin_metadata.clear()
    session_binding._runtime_session_todo_snapshots.clear()
    session_binding._pending_outbound_injections.clear()
    yield
    session_binding._mcp_runtime_sessions.clear()
    session_binding._runtime_session_origin_metadata.clear()
    session_binding._runtime_session_todo_snapshots.clear()
    session_binding._pending_outbound_injections.clear()


class TestMcpToRuntimeSessionBinding:
    def test_bind_and_lookup_round_trip(self):
        session_binding.bind_mcp_session_to_runtime_session("mcp-1", "cli:default")
        assert session_binding.get_runtime_session_for_mcp_session("mcp-1") == "cli:default"

    def test_blank_inputs_are_ignored(self):
        session_binding.bind_mcp_session_to_runtime_session("  ", "cli:default")
        assert session_binding.get_runtime_session_for_mcp_session("  ") is None

    def test_missing_lookup_returns_none(self):
        assert session_binding.get_runtime_session_for_mcp_session("unknown") is None


class TestOriginMetadata:
    def test_round_trip_returns_deep_copy(self):
        metadata = {"chat_id": 123, "nested": {"k": "v"}}
        session_binding.bind_runtime_session_origin_metadata("cli:default", metadata)

        recovered = session_binding.get_runtime_session_origin_metadata("cli:default")
        assert recovered == metadata
        recovered["chat_id"] = 999  # mutate the copy
        assert session_binding.get_runtime_session_origin_metadata("cli:default") == metadata


class TestTodoSnapshot:
    def test_snapshot_is_stored_and_returned_as_copy(self):
        snapshot = TodoListSnapshot(items=[TodoItem(id=1, status="not-started", title="x")])
        session_binding.bind_runtime_session_todo_snapshot("cli:default", snapshot)

        recovered = session_binding.get_runtime_session_todo_snapshot("cli:default")
        assert recovered is not None
        assert recovered.items[0].title == "x"
        assert recovered is not snapshot

    def test_empty_or_none_snapshot_clears_state(self):
        snapshot = TodoListSnapshot(items=[TodoItem(id=1, status="not-started", title="x")])
        session_binding.bind_runtime_session_todo_snapshot("cli:default", snapshot)
        session_binding.bind_runtime_session_todo_snapshot("cli:default", None)
        assert session_binding.get_runtime_session_todo_snapshot("cli:default") is None


class TestPendingOutboundInjections:
    def test_record_then_consume_drains_the_queue(self):
        session_binding.record_pending_outbound_injection("a", "b")
        session_binding.record_pending_outbound_injection("a", "c")
        assert session_binding.consume_pending_outbound_injections("a") == ["b", "c"]
        assert session_binding.consume_pending_outbound_injections("a") == []


class TestSplitRuntimeSessionKey:
    def test_well_formed_key_is_split(self):
        assert session_binding.split_runtime_session_key("telegram:123") == ("telegram", "123")

    def test_missing_separator_returns_none(self):
        assert session_binding.split_runtime_session_key("telegram") is None

    def test_blank_components_return_none(self):
        assert session_binding.split_runtime_session_key(":123") is None
        assert session_binding.split_runtime_session_key("telegram:") is None
