"""Shared wall-clock helper."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)
