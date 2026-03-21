"""
Schema definitions for authenticated operator and control surfaces.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Self

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
