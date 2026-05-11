"""Security pattern loading and mtime-based reload."""

from __future__ import annotations

import json
import os
import time

import pytest

from app.runtime.pipeline import (
    InboundProcessingPipeline,
    _load_security_patterns_from_disk,
    ensure_security_patterns_file,
)
from app.schema.messages import InboundBatch, InboundMessage


def _batch(text: str) -> InboundBatch:
    return InboundBatch(
        messages=(InboundMessage(channel_name="cli", conversation_id="default", user_id="u-1", text=text),),
    )


def _write_patterns(path, warnings: list[dict] | None = None, blocks: list[dict] | None = None) -> None:
    path.write_text(
        json.dumps(
            {
                "warning_patterns": warnings or [],
                "block_patterns": blocks or [],
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def clear_pattern_cache():
    _load_security_patterns_from_disk.cache_clear()
    yield
    _load_security_patterns_from_disk.cache_clear()


class TestEnsureSecurityPatternsFile:
    def test_seeds_default_file_when_missing(self, isolated_settings):
        path = isolated_settings.SECURITY_PATTERNS_PATH
        assert not path.exists()
        ensure_security_patterns_file()
        assert path.is_file()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["warning_patterns"], "default warnings should be seeded"
        assert payload["block_patterns"], "default blocks should be seeded"

    def test_existing_file_is_left_alone(self, isolated_settings):
        path = isolated_settings.SECURITY_PATTERNS_PATH
        _write_patterns(path, warnings=[{"name": "custom", "pattern": "(?i)custom"}])
        snapshot = path.read_text(encoding="utf-8")
        ensure_security_patterns_file()
        assert path.read_text(encoding="utf-8") == snapshot


class TestPatternMtimeReload:
    async def test_changing_file_triggers_reload(self, isolated_settings):
        path = isolated_settings.SECURITY_PATTERNS_PATH
        _write_patterns(path, warnings=[{"name": "phase-one", "pattern": "(?i)\\balpha\\b"}])

        pipeline = InboundProcessingPipeline()
        first = await pipeline.process(_batch("alpha please"))
        assert "phase-one" in first.security.warnings

        # Bump mtime past the LRU cache key by writing a fresh pattern; sleep a hair
        # past mtime_ns resolution so the cache key actually changes on filesystems
        # with coarse granularity (CI runners with ext4 typically have ns precision,
        # but be defensive).
        time.sleep(0.01)
        _write_patterns(path, warnings=[{"name": "phase-two", "pattern": "(?i)\\bbeta\\b"}])
        new_mtime = path.stat().st_mtime_ns
        if new_mtime == path.stat().st_mtime_ns:
            os.utime(path, ns=(new_mtime + 1_000_000, new_mtime + 1_000_000))

        second = await pipeline.process(_batch("beta please"))
        assert "phase-two" in second.security.warnings
        assert "phase-one" not in second.security.warnings

    async def test_invalid_pattern_is_skipped_without_crashing(self, isolated_settings):
        path = isolated_settings.SECURITY_PATTERNS_PATH
        _write_patterns(
            path,
            warnings=[
                {"name": "broken", "pattern": "(unclosed"},
                {"name": "ok", "pattern": "(?i)\\bgolden\\b"},
            ],
        )
        processed = await InboundProcessingPipeline().process(_batch("golden moment"))
        assert "ok" in processed.security.warnings
        assert "broken" not in processed.security.warnings
