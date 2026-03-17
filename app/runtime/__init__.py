"""
Runtime primitives for the Pillbug application loop.
"""

from typing import Any

__all__ = ("ApplicationLoop",)


def __getattr__(name: str) -> Any:
    if name == "ApplicationLoop":
        from app.runtime.loop import ApplicationLoop

        return ApplicationLoop

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
