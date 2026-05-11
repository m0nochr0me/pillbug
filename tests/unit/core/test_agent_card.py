"""Agent card builder: public vs extended card and bearer-gated capabilities."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.core.agent_card import build_extended_agent_card, build_public_agent_card
from app.core.config import settings


@pytest.fixture
def workspace_with_skill(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "skills" / "custom"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: custom-skill\ndescription: a custom thing\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", workspace, raising=True)
    return workspace


class TestPublicAgentCard:
    def test_extended_capability_is_false_without_bearer(self, monkeypatch):
        monkeypatch.setattr(settings, "A2A_BEARER_TOKEN", None, raising=True)
        card = build_public_agent_card()
        assert card.capabilities.extended_agent_card is False
        assert card.security_schemes is None

    def test_extended_capability_is_true_when_bearer_configured(self, monkeypatch):
        monkeypatch.setattr(
            settings,
            "A2A_BEARER_TOKEN",
            SecretStr("a2a-bearer-token-1234567890"),
            raising=True,
        )
        card = build_public_agent_card()
        assert card.capabilities.extended_agent_card is True
        assert card.security_schemes is not None
        assert "a2aBearer" in card.security_schemes

    def test_built_in_skills_are_present(self, monkeypatch):
        monkeypatch.setattr(settings, "A2A_BEARER_TOKEN", None, raising=True)
        card = build_public_agent_card()
        skill_ids = {skill.id for skill in card.skills}
        assert {"web-context-fetching", "planning-and-scheduling", "cross-runtime-delegation"} <= skill_ids


class TestExtendedAgentCard:
    def test_returns_none_without_bearer(self, monkeypatch):
        monkeypatch.setattr(settings, "A2A_BEARER_TOKEN", None, raising=True)
        assert build_extended_agent_card() is None

    def test_returns_card_with_bearer(self, monkeypatch):
        monkeypatch.setattr(
            settings,
            "A2A_BEARER_TOKEN",
            SecretStr("a2a-bearer-token-1234567890"),
            raising=True,
        )
        card = build_extended_agent_card()
        assert card is not None
        assert card.capabilities.extended_agent_card is True


class TestSkillDiscoveryIntegration:
    def test_workspace_skills_are_surfaced_in_agent_card(self, monkeypatch, workspace_with_skill):
        monkeypatch.setattr(settings, "A2A_BEARER_TOKEN", None, raising=True)
        card = build_public_agent_card()
        custom = [skill for skill in card.skills if skill.name == "custom-skill"]
        assert custom, "expected workspace skill to surface in agent card"
        assert custom[0].id.startswith("workspace-skill-")
