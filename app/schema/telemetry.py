"""
Schema definitions for runtime telemetry payloads.
"""

from datetime import UTC, datetime

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RuntimeMetadata(BaseModel):
    runtime_id: str = Field(min_length=1, description="Stable runtime identifier for this isolated Pillbug instance.")
    project: str = Field(min_length=1, description="The runtime project name.")
    version: str = Field(min_length=1, description="The running application version.")
    agent_name: str | None = Field(default=None, description="Optional operator-facing agent name, when configured.")
    started_at: datetime = Field(
        default_factory=_utcnow,
        description="UTC timestamp when this runtime boot record was created.",
    )
    timezone: str = Field(min_length=1, description="Configured runtime timezone.")
    workspace_root: str = Field(
        min_length=1, description="Absolute workspace root path enforced by the MCP file tools."
    )
    enabled_channels: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Enabled inbound channels for this runtime instance.",
    )
