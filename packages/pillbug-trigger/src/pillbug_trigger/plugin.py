"""Register the trigger management MCP tool against the runtime FastMCP server."""

from typing import TYPE_CHECKING, Any, Literal

from fastmcp import FastMCP

from pillbug_trigger import store
from pillbug_trigger.config import settings
from pillbug_trigger.schema import TriggerSourceConfig, Urgency

if TYPE_CHECKING:
    from app.runtime.mcp_plugins import McpRegistrationContext

__all__ = ("register_trigger_tools",)


def _serialize(config: TriggerSourceConfig) -> dict[str, Any]:
    return config.model_dump(mode="json")


def register_trigger_tools(mcp: FastMCP, ctx: McpRegistrationContext) -> None:
    """Register the single `manage_trigger_sources` CRUD tool.

    Self-gates on the trigger channel being enabled, so disabled deployments
    don't expose the surface even when this plugin is listed in
    `PB_MCP_TOOL_FACTORIES`.
    """

    if "trigger" not in ctx.settings.enabled_channels():
        ctx.logger.info("Skipping pillbug-trigger MCP tool registration: 'trigger' channel is not enabled")
        return

    @mcp.tool
    async def manage_trigger_sources(
        action: Literal["list", "get", "create", "update", "delete"],
        source: str | None = None,
        prompt: str | None = None,
        urgency_override: Urgency | None = None,
    ) -> dict[str, Any]:
        """
        Creates, lists, reads, updates, and deletes per-source trigger reaction prompts.

        Trigger sources control how the agent reacts to events posted to the trigger HTTP endpoint.
        Each source has a prompt with optional {title} and {body} placeholders and an optional
        urgency_override that takes precedence over the incoming event urgency.

        Source is required for get, create, update, and delete.
        Prompt is required for create; update accepts a partial change.
        Passing urgency_override on update replaces the existing value; omit to keep it.
        Changes are persisted to the trigger sources file and picked up live by the running channel.
        """

        normalized_action = action.strip().lower()

        if normalized_action == "list":
            configs = await store.list_sources()
            return {
                "sources_path": str(settings.SOURCES_PATH),
                "count": len(configs),
                "items": [_serialize(cfg) for cfg in configs],
            }

        if not source or not source.strip():
            raise ValueError(f"source is required for {normalized_action}")
        source_id = source.strip()

        if normalized_action == "get":
            try:
                config = await store.get_source(source_id)
            except store.TriggerSourceNotFoundError as exc:
                raise ValueError(f"Trigger source not found: {source_id}") from exc
            return _serialize(config)

        if normalized_action == "create":
            if not prompt or not prompt.strip():
                raise ValueError("prompt is required for create")
            try:
                config = await store.create_source(source_id, prompt, urgency_override)
            except store.TriggerSourceAlreadyExistsError as exc:
                raise ValueError(f"Trigger source already exists: {source_id}") from exc
            return _serialize(config)

        if normalized_action == "update":
            if prompt is None and urgency_override is None:
                raise ValueError("update requires at least one of prompt or urgency_override")
            try:
                config = await store.update_source(source_id, prompt, urgency_override)
            except store.TriggerSourceNotFoundError as exc:
                raise ValueError(f"Trigger source not found: {source_id}") from exc
            return _serialize(config)

        if normalized_action == "delete":
            try:
                deleted = await store.delete_source(source_id)
            except store.TriggerSourceNotFoundError as exc:
                raise ValueError(f"Trigger source not found: {source_id}") from exc
            return {"deleted_source": deleted}

        raise ValueError(f"Unsupported action: {action}")
