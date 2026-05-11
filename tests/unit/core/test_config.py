"""PB_* environment loading and runtime identity validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**overrides) -> Settings:
    defaults = {
        "GEMINI_API_KEY": "test-key",
        "GEMINI_BACKEND": "developer",
        "RUNTIME_ID": "pillbug-test",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


class TestRuntimeIdValidation:
    @pytest.mark.parametrize("value", ["abc", "pillbug-1", "rt_2.x"])
    def test_valid_runtime_ids_are_accepted(self, value: str):
        settings = _settings(RUNTIME_ID=value)
        assert settings.RUNTIME_ID == value

    @pytest.mark.parametrize("value", ["", "a", "ab", "!!", "-leading-dash", "rt id"])
    def test_invalid_runtime_ids_are_rejected(self, value: str):
        with pytest.raises(ValidationError):
            _settings(RUNTIME_ID=value)


class TestBearerTokenValidation:
    def test_short_token_is_rejected(self):
        with pytest.raises(ValidationError):
            _settings(DASHBOARD_BEARER_TOKEN="short")

    def test_blank_token_is_rejected(self):
        with pytest.raises(ValidationError):
            _settings(DASHBOARD_BEARER_TOKEN="   ")

    def test_dashboard_and_a2a_tokens_must_differ(self):
        with pytest.raises(ValidationError, match="must differ"):
            _settings(
                DASHBOARD_BEARER_TOKEN="same-token-1234567890",
                A2A_BEARER_TOKEN="same-token-1234567890",
            )

    def test_distinct_tokens_are_accepted(self):
        settings = _settings(
            DASHBOARD_BEARER_TOKEN="dashboard-token-1234",
            A2A_BEARER_TOKEN="a2a-token-1234567890",
        )
        assert settings.dashboard_bearer_token() == "dashboard-token-1234"
        assert settings.a2a_bearer_token() == "a2a-token-1234567890"


class TestGeminiBackendValidation:
    def test_developer_backend_requires_api_key(self):
        with pytest.raises(ValidationError, match="GEMINI_API_KEY"):
            Settings(GEMINI_BACKEND="developer", GEMINI_API_KEY=None)  # type: ignore[arg-type]

    def test_vertex_backend_requires_project_and_location(self):
        with pytest.raises(ValidationError, match="GEMINI_VERTEX"):
            Settings(GEMINI_BACKEND="vertex", GEMINI_API_KEY=None)  # type: ignore[arg-type]

    def test_vertex_rejects_base_url(self):
        with pytest.raises(ValidationError, match="GEMINI_BASE_URL"):
            Settings(  # type: ignore[arg-type]
                GEMINI_BACKEND="vertex",
                GEMINI_VERTEX_PROJECT="p",
                GEMINI_VERTEX_LOCATION="us-central1",
                GEMINI_BASE_URL="http://example.invalid",
            )


class TestChannelPluginFactories:
    def test_malformed_entry_is_rejected(self):
        settings = _settings(CHANNEL_PLUGIN_FACTORIES="bogus-no-equals")
        with pytest.raises(ValueError, match="channel=package.module:factory"):
            settings.channel_plugin_factories()

    def test_well_formed_entries_are_parsed(self):
        settings = _settings(CHANNEL_PLUGIN_FACTORIES="cli=app.runtime.channels:CliChannel,foo=pkg.mod:make")
        mappings = settings.channel_plugin_factories()
        assert mappings == {"cli": "app.runtime.channels:CliChannel", "foo": "pkg.mod:make"}


class TestEnabledChannels:
    def test_csv_is_parsed(self):
        settings = _settings(ENABLED_CHANNELS="cli, telegram,a2a")
        assert settings.enabled_channels() == ("cli", "telegram", "a2a")

    def test_default_falls_back_to_cli(self):
        settings = _settings(ENABLED_CHANNELS="   ,   ")
        assert settings.enabled_channels() == ("cli",)


class TestDocketUrl:
    def test_memory_when_no_redis_or_explicit_url(self):
        settings = _settings()
        assert settings.docket_url() == "memory://"

    def test_explicit_docket_url_wins(self):
        settings = _settings(DOCKET_URL="redis://example/0")
        assert settings.docket_url() == "redis://example/0"


class TestRuntimeIdentityFromFile:
    def test_runtime_id_is_persisted_when_not_configured(self, tmp_path: Path):
        runtime_id_path = tmp_path / "runtime_id.txt"
        settings = Settings(  # type: ignore[arg-type]
            BASE_DIR=tmp_path,
            RUNTIME_ID_PATH=runtime_id_path,
            RUNTIME_ID=None,
            GEMINI_API_KEY="test-key",
        )
        runtime_id = settings.ensure_runtime_identity()
        assert runtime_id.startswith("pillbug-")
        assert runtime_id_path.read_text(encoding="utf-8").strip() == runtime_id

    def test_existing_runtime_id_file_is_reused(self, tmp_path: Path):
        runtime_id_path = tmp_path / "runtime_id.txt"
        runtime_id_path.write_text("pillbug-existing\n", encoding="utf-8")
        settings = Settings(  # type: ignore[arg-type]
            BASE_DIR=tmp_path,
            RUNTIME_ID_PATH=runtime_id_path,
            RUNTIME_ID=None,
            GEMINI_API_KEY="test-key",
        )
        assert settings.ensure_runtime_identity() == "pillbug-existing"
