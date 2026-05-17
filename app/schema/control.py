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


ApprovalStatus = Literal["pending", "approved", "denied", "used"]


class ApprovalRequest(BaseModel):
    """Persisted command draft awaiting operator decision (plan P0 #2)."""

    id: str = Field(min_length=1, description="Opaque, single-use draft identifier.")
    command: str = Field(min_length=1, description="Exact shell command the model proposes to run.")
    justification: str = Field(
        min_length=1,
        description="Model-supplied rationale shown to the operator at decision time.",
    )
    directory: str = Field(default=".", description="Workspace-relative directory in which to execute.")
    timeout_seconds: float | None = Field(
        default=None,
        description="Optional override for the subprocess timeout; None falls back to the default at run time.",
    )
    status: ApprovalStatus = Field(default="pending", description="Current state of the draft.")
    source: str = Field(
        default="mcp",
        description="Identifier of the surface that drafted the command (e.g. 'mcp', session key).",
    )
    requested_at: datetime = Field(default_factory=_utcnow)
    decided_at: datetime | None = None
    decided_by: str | None = Field(default=None, description="Auth scope or principal that decided the draft.")
    decided_comment: str | None = Field(default=None, max_length=2000)
    used_at: datetime | None = None


class ApprovalDecision(BaseModel):
    """Operator-supplied payload for /control/approvals/{id}/approve | deny."""

    comment: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def normalize_comment(self) -> Self:
        if self.comment is not None:
            stripped = self.comment.strip()
            self.comment = stripped or None
        return self


class ApprovedAction(BaseModel):
    """Compact projection of an approved or used command draft for audit surfaces."""

    draft_id: str
    command: str
    status: ApprovalStatus
    decided_by: str | None = None
    decided_at: datetime | None = None
    used_at: datetime | None = None


class OutboundDraftKind(StrEnum):
    SEND_MESSAGE = "send_message"
    SEND_FILE = "send_file"
    SEND_A2A_MESSAGE = "send_a2a_message"
    REQUEST_A2A_RESPONSE = "request_a2a_response"


OutboundDraftStatus = Literal["pending", "committed", "discarded"]


class OutboundAttachmentDraft(BaseModel):
    path: str = Field(min_length=1, description="Workspace-relative or absolute path to the attached file.")
    caption: str | None = Field(default=None)
    send_as: str | None = Field(default=None)


class OutboundDraft(BaseModel):
    """Persisted outbound-send draft awaiting auto-send check or operator commit (plan P0 #3)."""

    id: str = Field(min_length=1)
    kind: OutboundDraftKind = Field(description="Which outbound surface this draft targets.")
    channel: str = Field(
        min_length=1,
        description="Bare channel name used for the autosend-allowlist check (e.g. 'cli', 'telegram', 'a2a').",
    )
    target: str = Field(
        default="",
        description=(
            "Full destination string. For send_message/send_file this may be empty or a "
            "session-style 'channel:conversation_id'; for A2A flows this is the normalized "
            "'runtime/conversation' target."
        ),
    )
    message: str = Field(default="", description="Outbound message text; may be empty when only an attachment is sent.")
    attachment: OutboundAttachmentDraft | None = Field(default=None)
    timeout_seconds: float | None = Field(
        default=None,
        description="Optional sync timeout (request_a2a_response only); ignored for async kinds.",
    )
    status: OutboundDraftStatus = "pending"
    source: str = Field(
        default="mcp",
        description="Identifier of the surface that drafted the message (e.g. 'mcp', session key).",
    )
    requested_at: datetime = Field(default_factory=_utcnow)
    decided_at: datetime | None = None
    decided_by: str | None = None
    decided_comment: str | None = Field(default=None, max_length=2000)


class OutboundDraftDecision(BaseModel):
    """Operator-supplied payload for /control/drafts/{id}/commit | discard."""

    comment: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def normalize_comment(self) -> Self:
        if self.comment is not None:
            stripped = self.comment.strip()
            self.comment = stripped or None
        return self


class PlanningModeRequest(BaseModel):
    """Operator-supplied payload for POST /control/sessions/{id}/planning-mode (plan P2 #11)."""

    state: Literal["planning", "normal"]
    objective: str | None = Field(default=None, description="Required when state=planning.")
    scope: str | None = Field(default=None)
    plan_summary: str | None = Field(
        default=None,
        description="Optional plan summary recorded when state=normal; defaults to an operator-cleared note.",
    )

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        if self.state == "planning":
            objective = (self.objective or "").strip()
            if not objective:
                raise ValueError("objective is required when state=planning")
            self.objective = objective
            self.scope = (self.scope.strip() if self.scope else None) or None
        else:
            if self.plan_summary is not None:
                summary = self.plan_summary.strip()
                self.plan_summary = summary or None
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
