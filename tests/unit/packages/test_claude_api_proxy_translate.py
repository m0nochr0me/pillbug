"""Regression tests for pillbug_claude_api_proxy.translate.extract_history."""

from __future__ import annotations

import pytest

translate = pytest.importorskip("pillbug_claude_api_proxy.translate")


def _fn_call(name: str, args: dict) -> dict:
    return {"functionCall": {"name": name, "args": args}}


def _fn_response(name: str, response: dict) -> dict:
    return {"functionResponse": {"name": name, "response": response}}


def test_orphan_tool_use_followed_by_user_text_gets_synthetic_tool_result() -> None:
    # Reproduces the production failure: google-genai AFC stopped after emitting
    # a functionCall, the next turn is just a user text prompt — Anthropic would
    # reject this as "tool_use ids were found without tool_result blocks".
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": "kick off the tool loop"}]},
            {"role": "model", "parts": [_fn_call("search", {"q": "x"})]},
            {"role": "user", "parts": [{"text": "what's taking so long?"}]},
        ]
    }

    messages = translate.extract_history(payload)

    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    tool_use_id = messages[1]["content"][0]["id"]
    follow_up = messages[2]["content"]
    assert follow_up[0] == {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": [{"type": "text", "text": "[no tool result was recorded for this call]"}],
        "is_error": True,
    }
    assert follow_up[1] == {"type": "text", "text": "what's taking so long?"}


def test_orphan_tool_use_as_last_message_gets_synthetic_user_message() -> None:
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": "go"}]},
            {"role": "model", "parts": [_fn_call("search", {"q": "x"})]},
        ]
    }

    messages = translate.extract_history(payload)

    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    tool_use_id = messages[1]["content"][0]["id"]
    assert messages[2]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": [{"type": "text", "text": "[no tool result was recorded for this call]"}],
            "is_error": True,
        }
    ]


def test_partial_tool_result_coverage_fills_in_missing_ids_only() -> None:
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": "do three things"}]},
            {
                "role": "model",
                "parts": [
                    _fn_call("a", {}),
                    _fn_call("b", {}),
                    _fn_call("c", {}),
                ],
            },
            {
                "role": "user",
                "parts": [
                    _fn_response("a", {"ok": True}),
                    _fn_response("c", {"ok": True}),
                ],
            },
        ]
    }

    messages = translate.extract_history(payload)

    tool_use_ids = [b["id"] for b in messages[1]["content"] if b["type"] == "tool_use"]
    assert tool_use_ids == ["toolu_00000001", "toolu_00000002", "toolu_00000003"]

    user_blocks = messages[2]["content"]
    covered = {b["tool_use_id"] for b in user_blocks if b["type"] == "tool_result"}
    assert covered == set(tool_use_ids)
    # The synthetic placeholder for "b" is_error=True; the genuine responses are not.
    by_id = {b["tool_use_id"]: b for b in user_blocks if b["type"] == "tool_result"}
    assert by_id["toolu_00000002"].get("is_error") is True
    assert by_id["toolu_00000001"].get("is_error") is not True
    assert by_id["toolu_00000003"].get("is_error") is not True


def test_well_formed_history_is_unchanged() -> None:
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": "go"}]},
            {"role": "model", "parts": [_fn_call("search", {"q": "x"})]},
            {"role": "user", "parts": [_fn_response("search", {"ok": True})]},
            {"role": "model", "parts": [{"text": "done"}]},
        ]
    }

    messages = translate.extract_history(payload)

    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert all(
        b.get("is_error") is not True
        for m in messages
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    )
