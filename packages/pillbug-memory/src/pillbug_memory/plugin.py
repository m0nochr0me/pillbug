"""Register flat-file memory tools against the runtime FastMCP server."""

from typing import TYPE_CHECKING, Any, Literal

from fastmcp import FastMCP

from pillbug_memory.schema import MemoryType
from pillbug_memory.store import MemoryNotFoundError, MemoryStore

if TYPE_CHECKING:
    from app.runtime.mcp_plugins import McpRegistrationContext

__all__ = ("register_memory_tools",)


def register_memory_tools(mcp: FastMCP, ctx: McpRegistrationContext) -> MemoryStore:
    """Register the five memory MCP tools and return the underlying store."""

    memory_dir = ctx.resolve_workspace_path(ctx.settings.MEMORY_DIR)
    memory_dir.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(memory_dir)
    ctx.logger.info(f"pillbug-memory store rooted at {memory_dir}")

    @mcp.tool
    async def memory_list(
        query: str | None = None,
        type: Literal["user", "feedback", "project", "reference"] | None = None,
        tag: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """
        Lists memories from the workspace memory store with optional filters.

        Substring match is applied to name and description (case-insensitive). Filters by
        memory type and exact tag membership. Returns frontmatter summaries only; use
        memory_get for full bodies.
        """

        records = await store.list(query=query, type=type, tag=tag, limit=limit)
        return {
            "count": len(records),
            "items": [MemoryStore.summarize(record) for record in records],
        }

    @mcp.tool
    async def memory_get(id: str) -> dict[str, Any]:
        """
        Retrieves a single memory by id (or unique id prefix), including its body.
        """

        try:
            record = await store.get(id)
        except MemoryNotFoundError as error:
            raise ValueError(str(error)) from error
        return MemoryStore.to_payload(record)

    @mcp.tool
    async def memory_add(
        name: str,
        description: str,
        body: str,
        type: MemoryType,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Stores a new memory file under the workspace memory directory.

        Use type=user|feedback|project|reference. Tags are optional free-form labels used
        for filtering with memory_list. Returns the created record including its assigned id.
        """

        record = await store.create(
            name=name,
            description=description,
            body=body,
            type=type,
            tags=tags,
        )
        return MemoryStore.to_payload(record)

    @mcp.tool
    async def memory_update(
        id: str,
        name: str | None = None,
        description: str | None = None,
        body: str | None = None,
        type: MemoryType | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Updates frontmatter fields and/or body of an existing memory. Only provided
        fields change; the updated timestamp is bumped automatically. Passing tags
        replaces the existing tag list.
        """

        try:
            record = await store.update(
                id,
                name=name,
                description=description,
                body=body,
                type=type,
                tags=tags,
            )
        except MemoryNotFoundError as error:
            raise ValueError(str(error)) from error
        return MemoryStore.to_payload(record)

    @mcp.tool
    async def memory_delete(id: str) -> dict[str, Any]:
        """
        Deletes a memory file and removes its entry from the MEMORY.md index.
        """

        try:
            deleted_id = await store.delete(id)
        except MemoryNotFoundError as error:
            raise ValueError(str(error)) from error
        return {"deleted_id": deleted_id}

    return store
