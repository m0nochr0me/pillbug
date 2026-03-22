"""
Schema definitions for authenticated operator and control surfaces.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AuthScope(StrEnum):
    TELEMETRY = "telemetry"
    CONTROL = "control"
    A2A = "a2a"


class AuthTokenBinding(BaseModel):
    token_name: str = Field(min_length=1, description="Stable internal label for a configured bearer token.")
    principal: Literal["dashboard", "a2a"] = Field(
        description="The class of caller that can present this bearer token."
    )
    scopes: tuple[AuthScope, ...] = Field(
        default_factory=tuple,
        description="The scopes granted by this token without exposing the token itself.",
    )

    @model_validator(mode="after")
    def validate_scopes(self) -> Self:
        unique_scopes = tuple(dict.fromkeys(self.scopes))
        if not unique_scopes:
            raise ValueError("AuthTokenBinding requires at least one scope")

        self.scopes = unique_scopes
        return self


class RuntimeAuthConfiguration(BaseModel):
    token_bindings: tuple[AuthTokenBinding, ...] = Field(
        default_factory=tuple,
        description="Configured auth bindings with non-secret scope metadata for telemetry, control, and A2A access.",
    )
    telemetry_protected: bool = Field(
        default=False,
        description="Whether read-only dashboard telemetry endpoints require bearer auth.",
    )
    control_protected: bool = Field(
        default=False,
        description="Whether operator control endpoints require bearer auth.",
    )
    a2a_protected: bool = Field(
        default=False,
        description="Whether runtime-to-runtime A2A ingress requires bearer auth.",
    )


class ControlMessageRequest(BaseModel):
    channel: str = Field(min_length=1, description="Enabled channel name to use for the outbound operator message.")
    conversation_id: str | None = Field(
        default=None,
        description="Optional explicit destination within the selected channel, such as a Telegram chat id.",
    )
    message: str = Field(min_length=1, description="Outbound message text to send.")

    @model_validator(mode="after")
    def normalize_values(self) -> Self:
        self.channel = self.channel.strip()
        self.message = self.message.strip()

        if not self.channel:
            raise ValueError("channel must not be blank")

        if ":" in self.channel:
            raise ValueError("channel must be provided without a destination suffix; use conversation_id instead")

        if self.conversation_id is not None:
            normalized_conversation_id = self.conversation_id.strip()
            self.conversation_id = normalized_conversation_id or None

        if not self.message:
            raise ValueError("message must not be blank")

        return self


class OperatorResponse(BaseModel):
    runtime_id: str = Field(min_length=1, description="The stable runtime identifier that produced this response.")
    ok: bool = Field(default=True, description="Whether the requested operator action was accepted.")
    action: str | None = Field(default=None, description="Optional action identifier such as clear, drain, or run-now.")
    message: str = Field(min_length=1, description="Human-readable response text for dashboards or operators.")
    scope: AuthScope | None = Field(default=None, description="The scope that authorized the response, if known.")
    responded_at: datetime = Field(
        default_factory=_utcnow,
        description="UTC timestamp when the response payload was created.",
    )
    details: dict[str, Any] | None = Field(
        default=None,
        description="Optional action-specific metadata that dashboards can use for follow-up UI state updates.",
    )
