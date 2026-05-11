"""Agent card payload schema invariants."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schema.agent_card import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HttpAuthSecurityScheme,
    SecurityScheme,
)


def _card(**overrides) -> AgentCard:
    defaults = {
        "name": "runtime-a",
        "description": "test runtime",
        "version": "0.1.0",
        "capabilities": AgentCapabilities(streaming=False, extended_agent_card=False),
        "supported_interfaces": (
            AgentInterface(url="http://localhost/a2a", protocol_binding="PILLBUG-A2A", protocol_version="1.0"),
        ),
    }
    defaults.update(overrides)
    return AgentCard(**defaults)


class TestSecurityScheme:
    def test_requires_exactly_one_scheme(self):
        with pytest.raises(ValidationError):
            SecurityScheme()

    def test_http_auth_scheme_round_trip(self):
        scheme = SecurityScheme(
            http_auth_security_scheme=HttpAuthSecurityScheme(scheme="Bearer", bearer_format="Opaque"),
        )
        assert scheme.http_auth_security_scheme is not None


class TestAgentCard:
    def test_camel_case_alias_is_emitted_by_default(self):
        card = _card()
        payload = card.model_dump(by_alias=True)
        assert "supportedInterfaces" in payload
        assert payload["supportedInterfaces"][0]["protocolBinding"] == "PILLBUG-A2A"

    def test_skill_round_trip(self):
        skill = AgentSkill(id="x", name="X", description="desc")
        card = _card(skills=(skill,))
        round_tripped = AgentCard.model_validate_json(card.model_dump_json(by_alias=True))
        assert round_tripped.skills[0].id == "x"
