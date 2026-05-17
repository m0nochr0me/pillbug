"""Unit tests for app.util.text.redact_secrets (plan P2 #16)."""

from __future__ import annotations

from app.util.text import redact_secrets


def test_json_api_key_value_is_redacted():
    redacted = redact_secrets('{"api_key": "sk-very-secret-token-123"}')
    assert "sk-very-secret-token-123" not in redacted
    assert "[REDACTED]" in redacted


def test_kwarg_token_value_is_redacted():
    assert redact_secrets("token=abcd1234efgh") == "token=[REDACTED]"


def test_case_insensitive_match():
    assert "[REDACTED]" in redact_secrets("Authorization: Bearer abc.def.ghi")


def test_unrelated_keys_are_unchanged():
    untouched = '{"name": "alice", "tool": "execute_command"}'
    assert redact_secrets(untouched) == untouched


def test_multiple_secrets_all_redacted():
    output = redact_secrets('{"api_key":"k1","password":"p2","secret":"s3"}')
    for needle in ("k1", "p2", "s3"):
        assert needle not in output
    assert output.count("[REDACTED]") == 3
