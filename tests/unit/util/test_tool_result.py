"""Unit tests for the structured tool-error envelope (plan P0 #4)."""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from app.util.tool_result import (
    ToolError,
    ToolErrorEnvelope,
    ToolErrorType,
    envelope_error,
    tool_error,
)


def test_tool_error_minimal_shape():
    payload = tool_error("not_found", "missing thing")

    assert payload == {
        "status": "error",
        "type": "not_found",
        "message": "missing thing",
        "next_valid_actions": [],
        "details": {},
    }


def test_tool_error_with_details_and_next_actions():
    payload = tool_error(
        "conflict",
        "stale write",
        next_valid_actions=("read_file", "retry"),
        details={"expected": 3, "got": 2},
    )

    assert payload["status"] == "error"
    assert payload["type"] == "conflict"
    assert payload["next_valid_actions"] == ["read_file", "retry"]
    assert payload["details"] == {"expected": 3, "got": 2}


def test_envelope_serialization_round_trips():
    envelope = ToolErrorEnvelope(
        type="approval_required",
        message="needs operator approval",
        next_valid_actions=("draft_command",),
        details={"draft_id": "abc123"},
    )
    payload = envelope.model_dump(mode="json")

    assert payload["status"] == "error"
    assert ToolErrorEnvelope.model_validate(payload) == envelope


def test_tool_error_type_enum_is_exhaustive():
    expected = {
        "invalid_arguments",
        "not_found",
        "permission_denied",
        "approval_required",
        "denied",
        "timeout",
        "rate_limited",
        "conflict",
        "already_used",
        "internal_error",
    }
    actual = set(get_args(ToolErrorType))
    assert actual == expected


def test_envelope_rejects_unknown_type():
    with pytest.raises(ValidationError):
        ToolErrorEnvelope(type="totally_made_up", message="nope")  # type: ignore[arg-type]


async def test_envelope_error_decorator_catches_value_error():
    @envelope_error
    async def tool_body():
        raise ValueError("bad input")

    result = await tool_body()
    assert result == {
        "status": "error",
        "type": "invalid_arguments",
        "message": "bad input",
        "next_valid_actions": [],
        "details": {},
    }


async def test_envelope_error_decorator_catches_tool_error():
    @envelope_error
    async def tool_body():
        raise ToolError(
            "denied",
            "no allowlist match",
            next_valid_actions=("draft_command",),
            details={"command": "rm -rf"},
        )

    result = await tool_body()
    assert result["type"] == "denied"
    assert result["message"] == "no allowlist match"
    assert result["next_valid_actions"] == ["draft_command"]
    assert result["details"] == {"command": "rm -rf"}


async def test_envelope_error_decorator_propagates_programmer_errors():
    @envelope_error
    async def tool_body():
        raise KeyError("missing_key")

    with pytest.raises(KeyError):
        await tool_body()


async def test_envelope_error_decorator_passes_success_through():
    @envelope_error
    async def tool_body():
        return {"ok": True, "value": 7}

    result = await tool_body()
    assert result == {"ok": True, "value": 7}
