"""Pydantic models shared across the dashboard package."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, SecretStr, model_validator


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RuntimeRegistration(BaseModel):
    runtime_id: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    label: str | None = None
    dashboard_bearer_token: SecretStr | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def normalize(self) -> RuntimeRegistration:
        self.runtime_id = self.runtime_id.strip()
        self.base_url = self.base_url.strip().rstrip("/")
        self.label = self.label.strip() if self.label else None

        if not self.runtime_id:
            raise ValueError("runtime_id must not be blank")

        if not self.base_url:
            raise ValueError("base_url must not be blank")

        return self

    def dashboard_bearer_token_value(self) -> str | None:
        if self.dashboard_bearer_token is None:
            return None
        return self.dashboard_bearer_token.get_secret_value()

    def to_storage(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "runtime_id": self.runtime_id,
            "base_url": self.base_url,
        }
        if self.label is not None:
            payload["label"] = self.label

        dashboard_bearer_token = self.dashboard_bearer_token_value()
        if dashboard_bearer_token is not None:
            payload["dashboard_bearer_token"] = dashboard_bearer_token

        return payload

    def to_public(self) -> RuntimeRegistrationPublic:
        return RuntimeRegistrationPublic(
            runtime_id=self.runtime_id,
            base_url=self.base_url,
            label=self.label,
            has_dashboard_bearer_token=self.dashboard_bearer_token is not None,
        )


class RuntimeRegistrationPublic(BaseModel):
    runtime_id: str
    base_url: str
    label: str | None = None
    has_dashboard_bearer_token: bool = False


class RuntimeRegistrationUpsert(BaseModel):
    runtime_id: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    label: str | None = None
    dashboard_bearer_token: str | None = None
    clear_dashboard_bearer_token: bool = False

    @model_validator(mode="after")
    def normalize(self) -> RuntimeRegistrationUpsert:
        self.runtime_id = self.runtime_id.strip()
        self.base_url = self.base_url.strip().rstrip("/")
        self.label = self.label.strip() if self.label else None

        if self.dashboard_bearer_token is not None:
            normalized_token = self.dashboard_bearer_token.strip()
            self.dashboard_bearer_token = normalized_token or None

        if not self.runtime_id:
            raise ValueError("runtime_id must not be blank")

        if not self.base_url:
            raise ValueError("base_url must not be blank")

        if self.clear_dashboard_bearer_token and self.dashboard_bearer_token is not None:
            raise ValueError("dashboard_bearer_token and clear_dashboard_bearer_token cannot be used together")

        return self


class RuntimeRegistrySnapshot(BaseModel):
    runtimes: list[RuntimeRegistration] = Field(default_factory=list)


class RuntimeConnectionStatus(BaseModel):
    connected: bool = False
    healthy: bool | None = None
    checked_at: datetime = Field(default_factory=_utcnow)
    error: str | None = None
    status_code: int | None = None


class DashboardSummary(BaseModel):
    total_runtimes: int = 0
    connected_runtimes: int = 0
    healthy_runtimes: int = 0
    degraded_runtimes: int = 0
    active_sessions: int = 0
    total_tasks: int = 0
    enabled_tasks: int = 0


class RuntimeOverview(BaseModel):
    registration: RuntimeRegistrationPublic
    status: RuntimeConnectionStatus
    health: dict[str, Any] | None = None
    runtime: dict[str, Any] | None = None
    channels: dict[str, Any] | None = None
    tasks: dict[str, Any] | None = None
    a2a_peers: tuple[str, ...] = Field(default_factory=tuple)


class RuntimeOverviewCollection(BaseModel):
    summary: DashboardSummary
    runtimes: list[RuntimeOverview] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=_utcnow)


class RuntimeDetailSnapshot(BaseModel):
    registration: RuntimeRegistrationPublic
    status: RuntimeConnectionStatus
    health: dict[str, Any] | None = None
    runtime: dict[str, Any] | None = None
    channels: dict[str, Any] | None = None
    sessions: dict[str, Any] | None = None
    tasks: dict[str, Any] | None = None
    agent_card: dict[str, Any] | None = None
    a2a_peers: tuple[str, ...] = Field(default_factory=tuple)
    generated_at: datetime = Field(default_factory=_utcnow)


class RegistryMutationResponse(BaseModel):
    ok: bool = True
    message: str
    registration: RuntimeRegistrationPublic | None = None
    generated_at: datetime = Field(default_factory=_utcnow)


class OutboundMessageRequest(BaseModel):
    channel: str = Field(min_length=1)
    conversation_id: str | None = None
    message: str = Field(min_length=1)

    @model_validator(mode="after")
    def normalize(self) -> OutboundMessageRequest:
        self.channel = self.channel.strip()
        self.message = self.message.strip()

        if self.conversation_id is not None:
            normalized_conversation_id = self.conversation_id.strip()
            self.conversation_id = normalized_conversation_id or None

        if not self.channel:
            raise ValueError("channel must not be blank")

        if ":" in self.channel:
            raise ValueError("channel must not include a destination suffix")

        if not self.message:
            raise ValueError("message must not be blank")

        return self
