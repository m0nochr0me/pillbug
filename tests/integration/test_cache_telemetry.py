"""Cache-token telemetry on the per-session response event (plan P1 #6)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import ai as ai_mod
from app.core.config import settings
from app.runtime.loop import ApplicationLoop, _SessionTelemetryState
from app.runtime.pipeline import InboundProcessingPipeline


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path, monkeypatch):
    monkeypatch.setattr(settings, "ENABLED_CHANNELS", "cli")
    monkeypatch.setattr(settings, "CHANNEL_PLUGIN_FACTORIES", "")
    (settings.WORKSPACE_ROOT / "AGENTS.md").write_text("# AGENTS.md\nstable\n", encoding="utf-8")
    return settings


@pytest.fixture
def application_loop(workspace_settings):
    service = ai_mod.GeminiChatService()
    loop = ApplicationLoop(chat_service=service, channels=[], pipeline=InboundProcessingPipeline())
    return loop


def _register_state(loop: ApplicationLoop, session_key: str = "cli:c1:u1") -> _SessionTelemetryState:
    state = _SessionTelemetryState(
        session_key=session_key,
        channel_name="cli",
        conversation_id="c1",
        user_id="u1",
        created_at=datetime.now(UTC),
    )
    loop._session_state_by_key[session_key] = state
    return state


def _usage(prompt: int, cached: int, output: int):
    return SimpleNamespace(
        prompt_token_count=prompt,
        cached_content_token_count=cached,
        candidates_token_count=output,
    )


class TestRecordCacheMetrics:
    def test_first_turn_populates_totals_and_per_turn_ratio(self, application_loop):
        state = _register_state(application_loop)
        metrics = application_loop._record_session_cache_metrics(
            state.session_key,
            usage_metadata=_usage(prompt=1000, cached=300, output=80),
            latency_ms=125.5,
        )

        assert metrics["prompt_tokens"] == 1000
        assert metrics["cached_content_tokens"] == 300
        assert metrics["output_tokens"] == 80
        assert metrics["cache_hit_ratio"] == pytest.approx(0.3)
        assert metrics["latency_ms"] == pytest.approx(125.5)

        assert state.cache_turn_count == 1
        assert state.cache_prompt_tokens == 1000
        assert state.cache_cached_tokens == 300
        assert state.cache_output_tokens == 80
        assert state.cache_last_turn_ratio == pytest.approx(0.3)
        assert state.cache_last_turn_latency_ms == pytest.approx(125.5)
        assert list(state.cache_recent_ratios) == pytest.approx([0.3])

    def test_zero_prompt_tokens_yields_none_ratio(self, application_loop):
        state = _register_state(application_loop)
        metrics = application_loop._record_session_cache_metrics(
            state.session_key,
            usage_metadata=_usage(prompt=0, cached=0, output=0),
            latency_ms=10.0,
        )
        assert metrics["cache_hit_ratio"] is None
        assert len(state.cache_recent_ratios) == 0  # nothing appended when prompt is 0

    def test_missing_usage_metadata_handled_gracefully(self, application_loop):
        state = _register_state(application_loop)
        metrics = application_loop._record_session_cache_metrics(
            state.session_key,
            usage_metadata=None,
            latency_ms=8.0,
        )
        assert metrics["prompt_tokens"] == 0
        assert metrics["cached_content_tokens"] == 0
        assert metrics["cache_hit_ratio"] is None
        assert state.cache_turn_count == 1


class TestCacheSummaryProjection:
    def test_cache_summary_none_until_first_turn(self, application_loop):
        state = _register_state(application_loop)
        assert application_loop._cache_summary_for(state) is None

    def test_cache_summary_populated_after_turns(self, application_loop):
        state = _register_state(application_loop)
        application_loop._record_session_cache_metrics(
            state.session_key,
            usage_metadata=_usage(prompt=1000, cached=400, output=50),
            latency_ms=100.0,
        )
        application_loop._record_session_cache_metrics(
            state.session_key,
            usage_metadata=_usage(prompt=2000, cached=600, output=70),
            latency_ms=200.0,
        )

        summary = application_loop._cache_summary_for(state)
        assert summary is not None
        assert summary.turn_count == 2
        assert summary.prompt_tokens == 3000
        assert summary.cached_content_tokens == 1000
        assert summary.output_tokens == 120
        assert summary.cache_hit_ratio == pytest.approx(1000 / 3000)
        assert summary.last_turn_latency_ms == pytest.approx(200.0)
        assert summary.last_turn_cache_hit_ratio == pytest.approx(0.3)


class TestLowHitRatioWarning:
    async def test_no_warning_until_window_full(self, monkeypatch, application_loop):
        monkeypatch.setattr(settings, "CACHE_HIT_RATIO_WARN_WINDOW", 3)
        monkeypatch.setattr(settings, "CACHE_HIT_RATIO_WARN_THRESHOLD", 0.3)
        state = _register_state(application_loop)

        for _ in range(2):
            application_loop._record_session_cache_metrics(
                state.session_key,
                usage_metadata=_usage(prompt=1000, cached=100, output=10),
                latency_ms=10.0,
            )
        window_ratio, should_warn = application_loop._maybe_warn_cache_hit_ratio(state.session_key)
        # window not full yet → no warning even though ratio is low
        assert should_warn is False
        assert window_ratio == pytest.approx(0.1)

    async def test_warning_fires_once_when_window_below_threshold(self, monkeypatch, application_loop):
        monkeypatch.setattr(settings, "CACHE_HIT_RATIO_WARN_WINDOW", 3)
        monkeypatch.setattr(settings, "CACHE_HIT_RATIO_WARN_THRESHOLD", 0.3)
        state = _register_state(application_loop)

        for _ in range(3):
            application_loop._record_session_cache_metrics(
                state.session_key,
                usage_metadata=_usage(prompt=1000, cached=100, output=10),
                latency_ms=10.0,
            )

        window_ratio, should_warn = application_loop._maybe_warn_cache_hit_ratio(state.session_key)
        assert should_warn is True
        assert window_ratio == pytest.approx(0.1)

        # Second check while still below threshold does NOT re-emit (already warned)
        _, should_warn_again = application_loop._maybe_warn_cache_hit_ratio(state.session_key)
        assert should_warn_again is False

    async def test_warning_re_arms_after_recovery(self, monkeypatch, application_loop):
        monkeypatch.setattr(settings, "CACHE_HIT_RATIO_WARN_WINDOW", 2)
        monkeypatch.setattr(settings, "CACHE_HIT_RATIO_WARN_THRESHOLD", 0.3)
        state = _register_state(application_loop)

        for _ in range(2):
            application_loop._record_session_cache_metrics(
                state.session_key,
                usage_metadata=_usage(prompt=1000, cached=100, output=10),
                latency_ms=10.0,
            )
        _, should_warn = application_loop._maybe_warn_cache_hit_ratio(state.session_key)
        assert should_warn is True

        # Recover above threshold for a window → warning re-arms
        for _ in range(2):
            application_loop._record_session_cache_metrics(
                state.session_key,
                usage_metadata=_usage(prompt=1000, cached=800, output=10),
                latency_ms=10.0,
            )
        _, should_warn_after_recovery = application_loop._maybe_warn_cache_hit_ratio(state.session_key)
        assert should_warn_after_recovery is False

        # Drop again → warning fires again
        for _ in range(2):
            application_loop._record_session_cache_metrics(
                state.session_key,
                usage_metadata=_usage(prompt=1000, cached=100, output=10),
                latency_ms=10.0,
            )
        _, should_warn_drop = application_loop._maybe_warn_cache_hit_ratio(state.session_key)
        assert should_warn_drop is True


class TestSessionsTelemetrySnapshot:
    async def test_cache_summary_surfaces_in_snapshot(self, application_loop):
        state = _register_state(application_loop)
        application_loop._record_session_cache_metrics(
            state.session_key,
            usage_metadata=_usage(prompt=1000, cached=400, output=50),
            latency_ms=42.0,
        )

        snapshot = await application_loop.describe_sessions_telemetry()
        assert len(snapshot.sessions) == 1
        entry = snapshot.sessions[0]
        assert entry.cache_summary is not None
        assert entry.cache_summary.turn_count == 1
        assert entry.cache_summary.cache_hit_ratio == pytest.approx(0.4)
        assert entry.cache_summary.last_turn_latency_ms == pytest.approx(42.0)
