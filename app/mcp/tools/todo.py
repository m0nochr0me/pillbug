"""Session-scoped todo planning MCP tool."""

from typing import Annotated, Any, Literal

from fastmcp import Context
from pydantic import Field, ValidationError

from app.core.log import logger
from app.mcp.server import (
    mcp,
)
from app.runtime.session_binding import (
    bind_runtime_session_todo_snapshot,
    get_runtime_session_for_mcp_session,
)
from app.schema.todo import TodoListSnapshot
from app.util.tool_result import envelope_error, tool_error

_TODO_LIST_STATE_KEY = "todo_list"


async def _get_todo_snapshot(ctx: Context) -> TodoListSnapshot:
    state = await ctx.get_state(_TODO_LIST_STATE_KEY)
    if state is None:
        _sync_todo_snapshot_to_runtime_session(ctx, None)
        return TodoListSnapshot()

    snapshot = TodoListSnapshot.model_validate(state)
    _sync_todo_snapshot_to_runtime_session(ctx, snapshot)
    return snapshot


def _sync_todo_snapshot_to_runtime_session(ctx: Context | None, snapshot: TodoListSnapshot | None) -> None:
    if ctx is None:
        return

    runtime_session_key = get_runtime_session_for_mcp_session(ctx.session_id)
    if runtime_session_key is None:
        return

    bind_runtime_session_todo_snapshot(runtime_session_key, snapshot)


def _serialize_todo_snapshot(action: str, snapshot: TodoListSnapshot) -> dict[str, Any]:
    return {
        "action": action,
        "items": [item.model_dump(mode="json") for item in snapshot.items],
        "total": len(snapshot.items),
        "counts": snapshot.counts,
        "explanation": snapshot.explanation,
        "updated_at": snapshot.updated_at.isoformat(),
    }


_TODO_ITEM_SCHEMA_HINT: dict[str, str] = {
    "id": "integer >= 1, unique within the list",
    "title": "non-empty string (NOT 'task' or 'content')",
    "status": "'not-started' | 'in-progress' | 'completed' (NOT 'pending')",
    "invariant": "at most one item may have status='in-progress'",
}


@mcp.tool
@envelope_error
async def manage_todo_list(
    action: Annotated[
        Literal["get", "set", "clear"],
        Field(description="get returns the current plan; set replaces it with `todo_list`; clear removes it."),
    ] = "get",
    todo_list: Annotated[
        list[dict[str, Any]] | None,
        Field(
            description=(
                "Required for action='set'. Full replacement of the plan. "
                "Each item MUST have these exact fields: "
                "id (integer >= 1, unique), "
                "title (non-empty string — do NOT use 'task' or 'content'), "
                "status (one of: 'not-started', 'in-progress', 'completed' — do NOT use 'pending'). "
                "At most one item may have status='in-progress'."
            ),
        ),
    ] = None,
    explanation: Annotated[
        str | None,
        Field(description="Optional human-readable note describing why the plan changed."),
    ] = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Stores and retrieves a session-scoped todo list for multi-step work.

    Actions:
      - "get" (default): return the current plan.
      - "set": replace the full plan with `todo_list`.
      - "clear": remove the plan.

    Item schema (exact field names — common slips like `task`, `content`, `pending` are rejected):
      - id: integer >= 1, unique within the list
      - title: non-empty string
      - status: one of "not-started", "in-progress", "completed"

    Invariants:
      - At most one item may have status="in-progress".
      - Duplicate ids are rejected.
    """

    normalized_action = action.strip().lower()

    if normalized_action == "get":
        snapshot = await _get_todo_snapshot(ctx)
        return _serialize_todo_snapshot("get", snapshot)

    if normalized_action == "clear":
        await ctx.delete_state(_TODO_LIST_STATE_KEY)
        _sync_todo_snapshot_to_runtime_session(ctx, None)
        return _serialize_todo_snapshot("clear", TodoListSnapshot())

    if normalized_action == "set":
        if todo_list is None:
            return tool_error(
                "invalid_arguments",
                "todo_list is required for set",
                next_valid_actions=("retry manage_todo_list with action='set' and a non-empty todo_list",),
                details={"item_schema": _TODO_ITEM_SCHEMA_HINT},
            )

        try:
            snapshot = TodoListSnapshot(items=todo_list, explanation=explanation)
        except ValidationError as exc:
            return tool_error(
                "invalid_arguments",
                "todo_list failed schema validation",
                next_valid_actions=("retry manage_todo_list set with corrected items",),
                details={
                    "errors": [
                        {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]}
                        for err in exc.errors(include_url=False, include_input=False)
                    ],
                    "item_schema": _TODO_ITEM_SCHEMA_HINT,
                },
            )

        await ctx.set_state(_TODO_LIST_STATE_KEY, snapshot.model_dump(mode="json"))
        _sync_todo_snapshot_to_runtime_session(ctx, snapshot)
        logger.debug(f"Updated todo list with {len(snapshot.items)} items")
        return _serialize_todo_snapshot("set", snapshot)

    return tool_error(
        "invalid_arguments",
        f"Unsupported action: {action}",
        details={"supported_actions": ["get", "set", "clear"]},
    )
