from typing import Any, Literal

from pydantic import AliasChoices, AnyUrl, BaseModel, ConfigDict, Field, field_validator


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
