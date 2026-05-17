"""Unit tests for OutboundSendBudget rolling-window counter (plan P2 #15)."""

from __future__ import annotations

import time

import pytest

from app.runtime.outbound_budget import OutboundSendBudget


def test_empty_limits_dict_is_always_allowed():
    budget = OutboundSendBudget()
    for _ in range(100):
        assert budget.check_and_charge("telegram", "abc", {}) is None


def test_per_session_cap_blocks_next_charge():
    budget = OutboundSendBudget()
    limits = {"per_session": 2}
    assert budget.check_and_charge("telegram", "abc", limits) is None
    assert budget.check_and_charge("telegram", "abc", limits) is None
    assert budget.check_and_charge("telegram", "abc", limits) == "per_session_exceeded"


def test_per_session_is_per_channel_target_pair():
    budget = OutboundSendBudget()
    limits = {"per_session": 1}
    assert budget.check_and_charge("telegram", "abc", limits) is None
    # Different conversation under the same channel: independent counter.
    assert budget.check_and_charge("telegram", "xyz", limits) is None
    # Different channel altogether: independent.
    assert budget.check_and_charge("a2a", "abc", limits) is None
    # Repeating the original pair trips the cap.
    assert budget.check_and_charge("telegram", "abc", limits) == "per_session_exceeded"


def test_per_minute_resets_after_window(monkeypatch):
    budget = OutboundSendBudget()
    limits = {"per_minute": 2}
    fake_now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_now[0])

    assert budget.check_and_charge("telegram", "abc", limits) is None
    assert budget.check_and_charge("telegram", "abc", limits) is None
    assert budget.check_and_charge("telegram", "abc", limits) == "per_minute_exceeded"

    fake_now[0] = 1061.0  # advance past 60s window
    assert budget.check_and_charge("telegram", "abc", limits) is None


def test_per_hour_takes_precedence_over_minute(monkeypatch):
    budget = OutboundSendBudget()
    limits = {"per_minute": 100, "per_hour": 3}
    fake_now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_now[0])

    for _ in range(3):
        assert budget.check_and_charge("telegram", "abc", limits) is None
    fake_now[0] += 120  # still inside the hour, past the minute
    assert budget.check_and_charge("telegram", "abc", limits) == "per_hour_exceeded"

    fake_now[0] += 3700  # past the hour window
    assert budget.check_and_charge("telegram", "abc", limits) is None


def test_rejected_call_does_not_charge():
    budget = OutboundSendBudget()
    limits = {"per_session": 1}
    assert budget.check_and_charge("telegram", "abc", limits) is None
    assert budget.check_and_charge("telegram", "abc", limits) == "per_session_exceeded"
    # The rejected call must not have consumed any window slot.
    # If we now raise the cap, we should still have only one charged send.
    raised = {"per_session": 2}
    assert budget.check_and_charge("telegram", "abc", raised) is None
    assert budget.check_and_charge("telegram", "abc", raised) == "per_session_exceeded"


def test_reset_clears_state():
    budget = OutboundSendBudget()
    limits = {"per_session": 1}
    assert budget.check_and_charge("telegram", "abc", limits) is None
    budget.reset()
    assert budget.check_and_charge("telegram", "abc", limits) is None


@pytest.mark.parametrize("invalid_window", ["per_decade", ""])
def test_unknown_window_is_ignored(invalid_window):
    # An unknown window key is simply not enforced — the budget only checks the keys it knows.
    budget = OutboundSendBudget()
    limits = {invalid_window: 0}
    assert budget.check_and_charge("telegram", "abc", limits) is None
