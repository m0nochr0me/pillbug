"""Schema definitions for A2A Agent Card discovery payloads."""

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True, use_enum_values=True)


class AgentProvider(_CamelModel):
    organization: str = Field(min_length=1)
    url: str = Field(min_length=1)


class HttpAuthSecurityScheme(_CamelModel):
    scheme: str = Field(min_length=1)
    description: str | None = None
    bearer_format: str | None = None


class SecurityScheme(_CamelModel):
    http_auth_security_scheme: HttpAuthSecurityScheme | None = None

    @model_validator(mode="after")
    def validate_union(self) -> Self:
        configured_fields = [
            field_name for field_name in ("http_auth_security_scheme",) if getattr(self, field_name) is not None
        ]
        if len(configured_fields) != 1:
            raise ValueError("SecurityScheme requires exactly one configured scheme")

        return self


class AgentExtension(_CamelModel):
    uri: str = Field(min_length=1)
    description: str | None = None
    required: bool | None = None
    params: dict[str, Any] | None = None


class AgentCapabilities(_CamelModel):
    streaming: bool | None = None
    push_notifications: bool | None = None
    extensions: tuple[AgentExtension, ...] | None = None
    extended_agent_card: bool | None = None


class AgentSkill(_CamelModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    tags: tuple[str, ...] = Field(default_factory=tuple)
    examples: tuple[str, ...] | None = None
    input_modes: tuple[str, ...] | None = None
    output_modes: tuple[str, ...] | None = None
    security_requirements: tuple[dict[str, tuple[str, ...]], ...] | None = None


class AgentInterface(_CamelModel):
    url: str = Field(min_length=1)
    protocol_binding: str = Field(min_length=1)
    protocol_version: str = Field(min_length=1)
    tenant: str | None = None


class AgentCardSignature(_CamelModel):
    protected: str = Field(min_length=1)
    signature: str = Field(min_length=1)
    header: dict[str, Any] | None = None


class AgentCard(_CamelModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    supported_interfaces: tuple[AgentInterface, ...] = Field(default_factory=tuple)
    provider: AgentProvider | None = None
    version: str = Field(min_length=1)
    documentation_url: str | None = None
    capabilities: AgentCapabilities
    security_schemes: dict[str, SecurityScheme] | None = None
    security: tuple[dict[str, tuple[str, ...]], ...] | None = None
    default_input_modes: tuple[str, ...] = Field(default_factory=tuple)
    default_output_modes: tuple[str, ...] = Field(default_factory=tuple)
    skills: tuple[AgentSkill, ...] = Field(default_factory=tuple)
    signatures: tuple[AgentCardSignature, ...] | None = None
    icon_url: str | None = None
