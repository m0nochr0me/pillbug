"""Operator control contract invariants."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schema.control import AuthScope, AuthTokenBinding, ControlMessageRequest


class TestAuthTokenBinding:
    def test_requires_at_least_one_scope(self):
        with pytest.raises(ValidationError):
            AuthTokenBinding(token_name="dash", principal="dashboard", scopes=())

    def test_deduplicates_scopes(self):
        binding = AuthTokenBinding(
            token_name="dash",
            principal="dashboard",
            scopes=(AuthScope.TELEMETRY, AuthScope.CONTROL, AuthScope.TELEMETRY),
        )
        assert binding.scopes == (AuthScope.TELEMETRY, AuthScope.CONTROL)


class TestControlMessageRequest:
    def test_channel_must_not_carry_destination_suffix(self):
        with pytest.raises(ValidationError):
            ControlMessageRequest(channel="telegram:123", message="hi")

    def test_blank_message_is_rejected(self):
        with pytest.raises(ValidationError):
            ControlMessageRequest(channel="cli", message="   ")

    def test_normalizes_blank_conversation_id_to_none(self):
        request = ControlMessageRequest(channel="cli", conversation_id="   ", message="hello")
        assert request.conversation_id is None
        assert request.message == "hello"
