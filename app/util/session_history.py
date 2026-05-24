"""Helpers for turning a Gemini chat history into operator-facing preview turns."""

from google.genai import types

from app.runtime.pipeline import redact_text_with_security_patterns
from app.schema.telemetry import SessionHistoryTurn


def _coerce_role(content: types.Content) -> str:
    role = getattr(content, "role", None)
    if isinstance(role, str) and role.strip():
        return role.strip()
    return "unknown"


def _serialize_turn(content: types.Content) -> SessionHistoryTurn:
    role = _coerce_role(content)
    parts = getattr(content, "parts", None) or ()

    text_segments: list[str] = []
    tool_call_names: list[str] = []
    tool_response_names: list[str] = []
    has_thought = False

    for part in parts:
        if getattr(part, "thought", False):
            has_thought = True
            continue

        function_call = getattr(part, "function_call", None)
        if function_call is not None:
            name = getattr(function_call, "name", None)
            if isinstance(name, str) and name:
                tool_call_names.append(name)
            continue

        function_response = getattr(part, "function_response", None)
        if function_response is not None:
            name = getattr(function_response, "name", None)
            if isinstance(name, str) and name:
                tool_response_names.append(name)
            continue

        part_text = getattr(part, "text", None)
        if isinstance(part_text, str) and part_text:
            text_segments.append(part_text)

    raw_text = "".join(text_segments)

    # Function-response parts arrive on the SDK's "user" role even though they're
    # tool output, which is misleading in the operator preview. Relabel as "tool"
    # when the turn carries nothing but tool responses.
    if tool_response_names and not raw_text and not tool_call_names:
        role = "tool"

    return SessionHistoryTurn(
        role=role,
        text=redact_text_with_security_patterns(raw_text),
        tool_call_names=tuple(tool_call_names),
        tool_response_names=tuple(tool_response_names),
        has_thought=has_thought,
    )


def _is_textless_tool_turn(turn: SessionHistoryTurn) -> bool:
    if turn.text or turn.has_thought:
        return False
    return bool(turn.tool_call_names) or bool(turn.tool_response_names)


def _merge_textless_runs(turns: list[SessionHistoryTurn]) -> list[SessionHistoryTurn]:
    """Collapse adjacent text-less tool turns into a single grouped entry per run."""
    merged: list[SessionHistoryTurn] = []
    run: list[SessionHistoryTurn] = []

    def flush_run() -> None:
        if not run:
            return
        if len(run) == 1:
            merged.append(run[0])
        else:
            call_names: list[str] = []
            response_names: list[str] = []
            for entry in run:
                call_names.extend(entry.tool_call_names)
                response_names.extend(entry.tool_response_names)
            merged.append(
                SessionHistoryTurn(
                    role="tool",
                    text="",
                    tool_call_names=tuple(call_names),
                    tool_response_names=tuple(response_names),
                    has_thought=False,
                    grouped_turn_count=len(run),
                )
            )
        run.clear()

    for turn in turns:
        if _is_textless_tool_turn(turn):
            run.append(turn)
            continue
        flush_run()
        merged.append(turn)

    flush_run()
    return merged


def serialize_history_tail(
    history: list[types.Content],
    *,
    limit: int,
) -> tuple[int, list[SessionHistoryTurn]]:
    """Serialize the most-recent ``limit`` turns from ``history`` with secret-pattern redaction.

    Adjacent text-less tool turns (function-call/response runs) are folded into a single
    grouped entry to keep the operator preview readable.

    Returns a tuple of ``(total_turn_count, turns)`` where ``turns`` is ordered
    oldest-first within the truncated window.
    """
    if limit <= 0:
        raise ValueError("limit must be greater than 0")

    total_turn_count = len(history)
    if total_turn_count == 0:
        return 0, []

    tail = history[-limit:]
    serialized = [_serialize_turn(content) for content in tail]
    return total_turn_count, _merge_textless_runs(serialized)
