"""
Per-session telemetry bookkeeping for the runtime loop.

`SessionTelemetryTracker` owns the per-session state map and the record/cache-ratio
logic that used to live on `ApplicationLoop`. The loop owns one tracker instance and
passes in a callable that counts still-pending inbound messages for a session.
"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from google.genai import types

from app.core.config import settings
from app.schema.messages import InboundBatch, InboundMessage
from app.schema.telemetry import CacheSummary
from app.util.clock import utcnow


@dataclass(slots=True)
class SessionTelemetryState:
    session_key: str
    channel_name: str
    conversation_id: str
    user_id: str | None
    created_at: datetime
    last_message_at: datetime | None = None
    last_response_at: datetime | None = None
    last_activity_at: datetime | None = None
    last_command: str | None = None
    message_count: int = 0
    pending_message_count: int = 0
    blocked_message_count: int = 0
    error_count: int = 0
    cache_turn_count: int = 0
    cache_prompt_tokens: int = 0
    cache_cached_tokens: int = 0
    cache_output_tokens: int = 0
    cache_last_turn_ratio: float | None = None
    cache_last_turn_latency_ms: float | None = None
    cache_recent_ratios: deque[float] = field(default_factory=deque)
    cache_low_hit_warning_emitted: bool = False


class SessionTelemetryTracker:
    def __init__(self, pending_count_for: Callable[[str], int]) -> None:
        self._pending_count_for = pending_count_for
        self.state_by_key: dict[str, SessionTelemetryState] = {}

    def get(self, session_key: str) -> SessionTelemetryState | None:
        return self.state_by_key.get(session_key)

    def state_for(
        self,
        *,
        session_key: str,
        channel_name: str,
        conversation_id: str,
        user_id: str | None,
        first_seen_at: datetime,
    ) -> SessionTelemetryState:
        state = self.state_by_key.get(session_key)
        if state is not None:
            if state.user_id is None and user_id is not None:
                state.user_id = user_id
            return state

        state = SessionTelemetryState(
            session_key=session_key,
            channel_name=channel_name,
            conversation_id=conversation_id,
            user_id=user_id,
            created_at=first_seen_at,
            last_activity_at=first_seen_at,
        )
        self.state_by_key[session_key] = state
        return state

    def sync_pending_count(self, session_key: str) -> None:
        state = self.state_by_key.get(session_key)
        if state is None:
            return

        state.pending_message_count = self._pending_count_for(session_key)

    def record_inbound_message(self, inbound_message: InboundMessage) -> None:
        state = self.state_for(
            session_key=inbound_message.session_key,
            channel_name=inbound_message.channel_name,
            conversation_id=inbound_message.conversation_id,
            user_id=inbound_message.user_id,
            first_seen_at=inbound_message.received_at,
        )
        state.message_count += 1
        state.last_message_at = inbound_message.received_at
        state.last_activity_at = inbound_message.received_at
        state.pending_message_count = self._pending_count_for(inbound_message.session_key)

    def record_blocked_batch(self, batch: InboundBatch) -> None:
        state = self.state_for(
            session_key=batch.session_key,
            channel_name=batch.channel_name,
            conversation_id=batch.conversation_id,
            user_id=batch.user_id,
            first_seen_at=batch.received_at,
        )
        state.blocked_message_count += batch.message_count
        state.last_activity_at = utcnow()
        self.sync_pending_count(batch.session_key)

    def record_command_invocation(self, batch: InboundBatch, command: str) -> None:
        state = self.state_for(
            session_key=batch.session_key,
            channel_name=batch.channel_name,
            conversation_id=batch.conversation_id,
            user_id=batch.user_id,
            first_seen_at=batch.received_at,
        )
        state.last_command = command
        state.last_activity_at = utcnow()

    def record_command_response(self, batch: InboundBatch, command: str) -> None:
        state = self.state_for(
            session_key=batch.session_key,
            channel_name=batch.channel_name,
            conversation_id=batch.conversation_id,
            user_id=batch.user_id,
            first_seen_at=batch.received_at,
        )
        now = utcnow()
        state.last_command = command
        state.last_response_at = now
        state.last_activity_at = now
        self.sync_pending_count(batch.session_key)

    def record_session_response(self, session_key: str) -> None:
        state = self.state_by_key.get(session_key)
        if state is None:
            return

        now = utcnow()
        state.last_response_at = now
        state.last_activity_at = now
        self.sync_pending_count(session_key)

    def record_session_activity(self, session_key: str) -> None:
        state = self.state_by_key.get(session_key)
        if state is None:
            return

        state.last_activity_at = utcnow()
        self.sync_pending_count(session_key)

    def record_session_error(self, session_key: str) -> None:
        state = self.state_by_key.get(session_key)
        if state is None:
            return

        state.error_count += 1
        state.last_activity_at = utcnow()
        self.sync_pending_count(session_key)

    def record_session_cache_metrics(
        self,
        session_key: str,
        *,
        usage_metadata: types.GenerateContentResponseUsageMetadata | None,
        latency_ms: float,
    ) -> dict[str, float | int | None]:
        """Update per-session cache totals and return a payload for the response telemetry event."""
        prompt_tokens = (usage_metadata.prompt_token_count or 0) if usage_metadata is not None else 0
        cached_tokens = (usage_metadata.cached_content_token_count or 0) if usage_metadata is not None else 0
        output_tokens = (usage_metadata.candidates_token_count or 0) if usage_metadata is not None else 0
        per_turn_ratio = cached_tokens / max(prompt_tokens, 1) if prompt_tokens else 0.0

        state = self.state_by_key.get(session_key)
        if state is not None:
            state.cache_turn_count += 1
            state.cache_prompt_tokens += prompt_tokens
            state.cache_cached_tokens += cached_tokens
            state.cache_output_tokens += output_tokens
            state.cache_last_turn_ratio = per_turn_ratio if prompt_tokens else None
            state.cache_last_turn_latency_ms = latency_ms

            window = max(settings.CACHE_HIT_RATIO_WARN_WINDOW, 1)
            if state.cache_recent_ratios.maxlen != window:
                state.cache_recent_ratios = deque(state.cache_recent_ratios, maxlen=window)
            if prompt_tokens:
                state.cache_recent_ratios.append(per_turn_ratio)

        return {
            "prompt_tokens": prompt_tokens,
            "cached_content_tokens": cached_tokens,
            "output_tokens": output_tokens,
            "cache_hit_ratio": per_turn_ratio if prompt_tokens else None,
            "latency_ms": latency_ms,
        }

    def maybe_warn_cache_hit_ratio(self, session_key: str) -> tuple[float | None, bool]:
        state = self.state_by_key.get(session_key)
        if state is None or not state.cache_recent_ratios:
            return None, False
        window = max(settings.CACHE_HIT_RATIO_WARN_WINDOW, 1)
        if len(state.cache_recent_ratios) < window:
            return sum(state.cache_recent_ratios) / len(state.cache_recent_ratios), False

        window_ratio = sum(state.cache_recent_ratios) / len(state.cache_recent_ratios)
        threshold = settings.CACHE_HIT_RATIO_WARN_THRESHOLD
        below_threshold = window_ratio < threshold
        should_emit = below_threshold and not state.cache_low_hit_warning_emitted
        if should_emit:
            state.cache_low_hit_warning_emitted = True
        elif not below_threshold:
            state.cache_low_hit_warning_emitted = False
        return window_ratio, should_emit

    def cache_summary_for(self, state: SessionTelemetryState) -> CacheSummary | None:
        if state.cache_turn_count == 0:
            return None
        cache_hit_ratio = state.cache_cached_tokens / max(state.cache_prompt_tokens, 1)
        window_ratio: float | None = None
        if state.cache_recent_ratios:
            window_ratio = sum(state.cache_recent_ratios) / len(state.cache_recent_ratios)
        return CacheSummary(
            turn_count=state.cache_turn_count,
            prompt_tokens=state.cache_prompt_tokens,
            cached_content_tokens=state.cache_cached_tokens,
            output_tokens=state.cache_output_tokens,
            cache_hit_ratio=cache_hit_ratio,
            last_turn_cache_hit_ratio=state.cache_last_turn_ratio,
            last_turn_latency_ms=state.cache_last_turn_latency_ms,
            window_hit_ratio=window_ratio,
        )
