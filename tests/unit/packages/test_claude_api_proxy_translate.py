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


def test_repeated_tool_name_after_orphan_does_not_steal_old_id() -> None:
    # Reproduces the second production failure: an earlier-orphaned tool_use for
    # name X must not "steal" the id from a later same-name tool_use's response.
    # Previously the translator matched against a global pending list, so the
    # late functionResponse popped the orphan's id and emitted a tool_result
    # whose tool_use_id lived many turns back — Anthropic 400.
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": "go"}]},
            {"role": "model", "parts": [_fn_call("search", {"q": "first"})]},
            {"role": "user", "parts": [{"text": "actually wait"}]},
            {"role": "model", "parts": [_fn_call("search", {"q": "second"})]},
            {"role": "user", "parts": [_fn_response("search", {"ok": True})]},
        ]
    }

    messages = translate.extract_history(payload)

    asst = [m for m in messages if m["role"] == "assistant"]
    assert len(asst) == 2
    first_id = asst[0]["content"][0]["id"]
    second_id = asst[1]["content"][0]["id"]
    assert first_id != second_id

    # The real functionResponse must pair with the SECOND (immediately previous) id.
    final_user = messages[-1]
    assert final_user["role"] == "user"
    real_results = [b for b in final_user["content"] if b.get("type") == "tool_result"]
    assert len(real_results) == 1
    assert real_results[0]["tool_use_id"] == second_id
    assert real_results[0].get("is_error") is not True

    # The orphan from the first model turn gets a synthetic placeholder in the
    # message immediately following it.
    orphan_followup = messages[2]
    assert orphan_followup["role"] == "user"
    placeholders = [b for b in orphan_followup["content"] if b.get("type") == "tool_result"]
    assert [b["tool_use_id"] for b in placeholders] == [first_id]
    assert placeholders[0]["is_error"] is True


def test_orphan_function_response_with_no_prior_call_becomes_text() -> None:
    # If a user turn carries a functionResponse for a tool the previous
    # assistant turn never called, we must NOT emit a tool_result (Anthropic
    # would 400). Preserve the payload as text so the model still sees it.
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": "hi"}]},
            {"role": "model", "parts": [{"text": "hello"}]},
            {"role": "user", "parts": [_fn_response("ghost", {"value": 42})]},
        ]
    }

    messages = translate.extract_history(payload)

    assert messages[-1]["role"] == "user"
    blocks = messages[-1]["content"]
    assert not any(b.get("type") == "tool_result" for b in blocks)
    assert any(b.get("type") == "text" and "ghost" in b.get("text", "") for b in blocks)


def test_generation_config_drops_top_p_when_temperature_also_set() -> None:
    # Claude 4+ models 400 if both temperature and top_p are present. Gemini
    # allows both, so the translator must reconcile to at most one.
    out = translate.extract_generation_config({"generationConfig": {"temperature": 0.7, "topP": 0.9}})
    assert out["temperature"] == 0.7
    assert "top_p" not in out


def test_generation_config_keeps_top_p_when_temperature_absent() -> None:
    out = translate.extract_generation_config({"generationConfig": {"topP": 0.9}})
    assert out == {"top_p": 0.9}


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


def _text_blocks(message: dict) -> list[dict]:
    return [b for b in message["content"] if isinstance(b, dict) and b.get("type") == "text"]


def test_empty_text_model_turn_does_not_emit_empty_anthropic_block() -> None:
    # Production failure: an empty model response (recorded as `{"text": ""}` in
    # Gemini history, which the runtime nudges past) reached the proxy and was
    # forwarded as an empty Anthropic text block. Anthropic 400s with
    # "messages: text content blocks must be non-empty".
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": "go"}]},
            {"role": "model", "parts": [_fn_call("search", {"q": "x"})]},
            {"role": "user", "parts": [_fn_response("search", {"ok": True})]},
            {"role": "model", "parts": [{"text": ""}]},
            {"role": "user", "parts": [{"text": "please continue"}]},
        ]
    }

    messages = translate.extract_history(payload)

    # No message carries an empty (or whitespace-only) text block.
    assert all(b["text"].strip() for m in messages for b in _text_blocks(m))
    # Roles still alternate after the empty assistant turn is dropped: the
    # tool_result turn and the nudge turn are merged into one user message.
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    final_user = messages[-1]
    assert any(b.get("type") == "tool_result" for b in final_user["content"])
    assert any(b.get("type") == "text" and "please continue" in b.get("text", "") for b in final_user["content"])


def test_whitespace_only_text_block_is_dropped() -> None:
    payload = {"contents": [{"role": "user", "parts": [{"text": "   \n  "}, {"text": "real question"}]}]}

    messages = translate.extract_history(payload)

    assert len(messages) == 1
    texts = [b["text"] for b in _text_blocks(messages[0])]
    assert texts == ["real question"]
