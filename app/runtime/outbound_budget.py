"""In-process per-channel outbound send budget (plan P2 #15).

Rolling-window counter keyed by `(channel, conversation_id)`. A send that would push
any of `per_session`, `per_minute`, or `per_hour` past its cap returns a labelled
reason so the call site can surface a `rate_limited` envelope. State is per-process —
the "session" window persists for the runtime's lifetime, which matches Pillbug's
single-runtime-per-container deployment shape.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Mapping
from threading import Lock

DEFAULT_NON_CLI_LIMITS: dict[str, int] = {
    "per_session": 20,
    "per_minute": 5,
    "per_hour": 60,
}


class OutboundSendBudget:
    def __init__(self) -> None:
        self._timestamps: dict[tuple[str, str], deque[float]] = {}
        self._per_session_counts: dict[tuple[str, str], int] = {}
        self._lock = Lock()

    def check_and_charge(
        self,
        channel: str,
        conversation_id: str,
        limits: Mapping[str, int],
    ) -> str | None:
        """Atomically check the limits and, if all pass, charge a send.

        Returns the violated-limit label (`per_session_exceeded` /
        `per_minute_exceeded` / `per_hour_exceeded`) when the call would exceed any cap;
        in that case nothing is charged. Returns None when the send is allowed and the
        counter has been incremented.
        """
        if not limits:
            return None
        key = (channel, conversation_id)
        now = time.monotonic()
        with self._lock:
            timestamps = self._timestamps.setdefault(key, deque())
            # Prune anything older than 1 hour (longest tracked window).
            while timestamps and now - timestamps[0] >= 3600:
                timestamps.popleft()

            session_count = self._per_session_counts.get(key, 0)
            minute_count = sum(1 for stamp in timestamps if now - stamp < 60)
            hour_count = len(timestamps)

            per_session = limits.get("per_session")
            per_minute = limits.get("per_minute")
            per_hour = limits.get("per_hour")

            if per_session is not None and session_count >= per_session:
                return "per_session_exceeded"
            if per_minute is not None and minute_count >= per_minute:
                return "per_minute_exceeded"
            if per_hour is not None and hour_count >= per_hour:
                return "per_hour_exceeded"

            timestamps.append(now)
            self._per_session_counts[key] = session_count + 1
            return None

    def reset(self) -> None:
        """Test helper: clear all counters."""
        with self._lock:
            self._timestamps.clear()
            self._per_session_counts.clear()


outbound_send_budget = OutboundSendBudget()
