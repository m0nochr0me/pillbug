"""Schema definitions for trigger events."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Urgency(StrEnum):
    LOW = "low"
    MED = "med"
    HIGH = "high"


URGENCY_DEBOUNCE_SECONDS: dict[Urgency, float] = {
    Urgency.LOW: 30.0,
    Urgency.MED: 10.0,
    Urgency.HIGH: 1.0,
}


class TriggerEvent(BaseModel):
    """An external event submitted to the trigger endpoint."""

    source: str = Field(description="Identifier for the trigger source (e.g. 'server-monitor', 'weather-alert')")
    urgency: Urgency = Field(default=Urgency.MED, description="Urgency level controls debounce window")
    title: str = Field(description="Short human-readable summary of the event")
    body: str = Field(default="", description="Detailed event payload or description")
    conversation_id: str | None = Field(
        default=None,
        description="Optional conversation to route to; defaults to source name",
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Arbitrary key-value metadata")


class TriggerSourceConfig(BaseModel):
    """Per-source configuration defining how the agent should react."""

    source: str = Field(description="Source identifier to match against incoming events")
    prompt: str = Field(
        description=(
            "Instruction prompt for the agent describing how to react to events from this source. "
            "Use {title} and {body} placeholders for event data."
        ),
    )
    urgency_override: Urgency | None = Field(
        default=None,
        description="Override the urgency from the event payload",
    )


class TriggerResponse(BaseModel):
    """Response returned after accepting a trigger event."""

    accepted: bool = True
    event_count: int = Field(description="Number of events currently pending in the debounce buffer for this key")
    debounce_seconds: float = Field(description="Debounce window applied to this event")
