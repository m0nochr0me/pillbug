from typing import Any, Literal

from pydantic import AliasChoices, AnyUrl, BaseModel, ConfigDict, Field, field_validator

from app.util.compaction import validate_compaction_stages


class MCPServer(BaseModel):
    name: str = Field(
        ...,
        description="Name of the MCP server",
    )
    type_: Literal["http"] | None = Field(
        "http",
        description="Type of the MCP server",
        alias="type",
    )
    url: AnyUrl = Field(
        ...,
        description="URL of the MCP server",
        alias="url",
        validation_alias=AliasChoices("url", "serverUrl"),
    )
    headers: dict[str, str] | None = Field(
        None,
        description="Optional headers to include in requests to the MCP server",
    )
    compacting: list[str] | None = Field(
        None,
        description=(
            "Optional list of compaction stages to apply to tool outputs from this "
            "server. Supported stages are slight, full, url_shorten, and "
            "rsub::<pattern>::<replacement>."
        ),
    )

    @field_validator("compacting")
    @classmethod
    def validate_compacting(cls, value: list[str] | None) -> list[str] | None:
        return validate_compaction_stages(value)

    model_config = ConfigDict(extra="ignore")


class MCPConfig(BaseModel):
    servers: dict[str, MCPServer] = Field(
        ...,
        description="List of MCP servers to use",
        alias="servers",
        validation_alias=AliasChoices("servers", "mcpServers"),
    )

    @field_validator("servers", mode="before")
    @classmethod
    def populate_server_names(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value

        servers: dict[str, Any] = {}
        for server_name, server_config in value.items():
            if not isinstance(server_config, dict):
                servers[server_name] = server_config
                continue

            servers[server_name] = {
                **server_config,
                "name": server_config.get("name", server_name),
            }

        return servers

    model_config = ConfigDict(extra="ignore")
