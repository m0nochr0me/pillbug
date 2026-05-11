"""Channel plugin factory resolution: malformed entries, dynamic imports, enablement."""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.runtime.channels import (
    CliChannel,
    _load_channel_factory,
    get_channel_plugin,
    load_channel_plugins,
    unregister_channel_plugin,
)


class TestLoadChannelFactory:
    def test_well_formed_path_resolves(self):
        factory = _load_channel_factory("app.runtime.channels:CliChannel")
        assert factory is CliChannel

    def test_missing_separator_is_rejected(self):
        with pytest.raises(ValueError, match="Invalid channel plugin factory path"):
            _load_channel_factory("app.runtime.channels.CliChannel")

    def test_empty_module_or_attribute_is_rejected(self):
        with pytest.raises(ValueError):
            _load_channel_factory(":CliChannel")
        with pytest.raises(ValueError):
            _load_channel_factory("app.runtime.channels:")

    def test_non_callable_attribute_is_rejected(self):
        with pytest.raises(TypeError, match="not callable"):
            _load_channel_factory("app.runtime.channels:_active_channels")


class TestLoadChannelPlugins:
    def test_default_loads_cli_channel(self, monkeypatch):
        monkeypatch.setattr(settings, "ENABLED_CHANNELS", "cli", raising=True)
        monkeypatch.setattr(settings, "CHANNEL_PLUGIN_FACTORIES", "", raising=True)
        # Drop the previously cached active CliChannel so the test starts clean.
        unregister_channel_plugin("cli")
        try:
            channels = load_channel_plugins()
            assert len(channels) == 1
            assert channels[0].name == "cli"
        finally:
            unregister_channel_plugin("cli")

    def test_unknown_channel_without_factory_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "ENABLED_CHANNELS", "ghost", raising=True)
        monkeypatch.setattr(settings, "CHANNEL_PLUGIN_FACTORIES", "", raising=True)
        with pytest.raises(ValueError, match="not available"):
            load_channel_plugins()


class TestGetChannelPlugin:
    def test_disabled_channel_returns_none(self, monkeypatch):
        monkeypatch.setattr(settings, "ENABLED_CHANNELS", "cli", raising=True)
        assert get_channel_plugin("ghost", create=True) is None
