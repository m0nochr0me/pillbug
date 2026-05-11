"""Pydantic invariants for inbound message schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schema.messages import (
    A2AConvergenceState,
    A2AEnvelope,
    A2AIntent,
    A2ATarget,
    InboundBatch,
    InboundMessage,
    build_a2a_origin_routing_metadata,
    extract_a2a_origin_route,
    extract_a2a_origin_routing_metadata,
)


def _make_inbound(channel: str = "cli", conversation: str = "default", user: str | None = "u-1", text: str = "hi"):
    return InboundMessage(channel_name=channel, conversation_id=conversation, user_id=user, text=text)


class TestInboundMessage:
    def test_session_key_combines_channel_and_conversation(self):
        message = _make_inbound(channel="telegram", conversation="abc")
        assert message.session_key == "telegram:abc"

    def test_debounce_key_includes_user(self):
        message = _make_inbound(channel="telegram", conversation="abc", user="u-42")
        assert message.debounce_key == "telegram:abc:u-42"

    def test_debounce_key_falls_back_to_anonymous_for_missing_user(self):
        message = _make_inbound(user=None)
        assert message.debounce_key.endswith(":anonymous")

    def test_distinct_users_produce_distinct_debounce_keys(self):
        a = _make_inbound(user="u-1")
        b = _make_inbound(user="u-2")
        assert a.debounce_key != b.debounce_key


class TestInboundBatch:
    def test_empty_batch_is_rejected(self):
        with pytest.raises(ValidationError):
            InboundBatch(messages=())

    def test_mixed_channel_is_rejected(self):
        a = _make_inbound(channel="cli")
        b = _make_inbound(channel="telegram")
        with pytest.raises(ValidationError):
            InboundBatch(messages=(a, b))

    def test_mixed_conversation_is_rejected(self):
        a = _make_inbound(conversation="x")
        b = _make_inbound(conversation="y")
        with pytest.raises(ValidationError):
            InboundBatch(messages=(a, b))

    def test_mixed_user_is_rejected(self):
        a = _make_inbound(user="u-1")
        b = _make_inbound(user="u-2")
        with pytest.raises(ValidationError):
            InboundBatch(messages=(a, b))

    def test_homogeneous_batch_is_accepted_and_exposes_aggregates(self):
        a = _make_inbound(text="one")
        b = _make_inbound(text="two")
        batch = InboundBatch(messages=(a, b))
        assert batch.message_count == 2
        assert batch.channel_name == "cli"
        assert batch.session_key == "cli:default"
        assert batch.raw_text == "one\n\ntwo"


class TestA2ATarget:
    def test_parse_accepts_prefixed_value(self):
        target = A2ATarget.parse("a2a:runtime-b/conv-1")
        assert target.runtime_id == "runtime-b"
        assert target.conversation_id == "conv-1"

    def test_parse_accepts_bare_value(self):
        target = A2ATarget.parse("runtime-b/conv-1")
        assert target.as_conversation_target() == "runtime-b/conv-1"

    def test_parse_rejects_missing_separator(self):
        with pytest.raises(ValueError):
            A2ATarget.parse("runtime-b")

    def test_invalid_runtime_id_is_rejected(self):
        with pytest.raises(ValidationError):
            A2ATarget(runtime_id="!!", conversation_id="conv-1")


class TestA2AConvergenceState:
    def test_hop_count_cannot_exceed_max_hops(self):
        with pytest.raises(ValidationError):
            A2AConvergenceState(max_hops=2, hop_count=3)

    def test_remaining_hops_tracks_max(self):
        state = A2AConvergenceState(max_hops=3, hop_count=1)
        assert state.remaining_hops == 2

    def test_terminal_intents_block_replies(self):
        state = A2AConvergenceState(max_hops=3, hop_count=0)
        assert state.reply_block_reason(A2AIntent.RESULT) == "terminal_intent"
        assert state.reply_block_reason(A2AIntent.ASK) is None

    def test_convergence_limit_blocks_reply(self):
        state = A2AConvergenceState(max_hops=2, hop_count=2)
        assert state.reply_block_reason(A2AIntent.ASK) == "convergence_limit"

    def test_stop_requested_takes_precedence(self):
        state = A2AConvergenceState(stop_requested=True)
        assert state.reply_block_reason(A2AIntent.ASK) == "stop_requested"


class TestA2AOriginRoutingMetadata:
    def test_round_trip_extracts_what_was_built(self):
        metadata = build_a2a_origin_routing_metadata(
            channel_name="telegram",
            conversation_id="123",
            channel_metadata={"chat_id": 123},
        )
        route = extract_a2a_origin_route(metadata)
        assert route == ("telegram", "123")

        extracted_pair = extract_a2a_origin_routing_metadata(metadata)
        assert extracted_pair is not None
        assert "telegram" in extracted_pair.values()

    def test_blank_channel_or_conversation_is_rejected(self):
        with pytest.raises(ValueError):
            build_a2a_origin_routing_metadata(channel_name="  ", conversation_id="x")

        with pytest.raises(ValueError):
            build_a2a_origin_routing_metadata(channel_name="x", conversation_id="  ")


class TestA2AEnvelope:
    def _envelope(self, **overrides) -> A2AEnvelope:
        defaults = {
            "sender_runtime_id": "runtime-a",
            "target_runtime_id": "runtime-b",
            "conversation_id": "conv-1",
            "text": "hello",
            "intent": A2AIntent.ASK,
        }
        defaults.update(overrides)
        return A2AEnvelope(**defaults)

    def test_blank_text_is_rejected(self):
        with pytest.raises(ValidationError):
            self._envelope(text="   ")

    def test_to_inbound_message_preserves_routing_fields(self):
        envelope = self._envelope()
        inbound = envelope.to_inbound_message()
        assert inbound.channel_name == "a2a"
        assert inbound.conversation_id == "runtime-a/conv-1"
        assert inbound.user_id == "runtime-a"
        assert inbound.metadata["a2a_intent"] == "ask"
