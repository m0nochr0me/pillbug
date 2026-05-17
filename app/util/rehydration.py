"""
Post-compaction rehydration bundle (plan P1 #9).

After `replace_history_with_summary` collapses the chat to a single synthetic turn,
the runtime appends a `RehydrationBundle`-rendered user turn so the model can resume
without losing live plan state, recent tool observations, loaded skills, or pending
approvals.

Cap the bundle at ~3-5KB so the net token impact of compression stays negative.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.schema.todo import TodoListSnapshot

__all__ = ("RehydrationBundle", "render_rehydration_text")


_REHYDRATION_HEADER = "RUNTIME REHYDRATION — do not restate, use to inform next action:"


class RehydrationBundle(BaseModel):
    """Compact, machine-readable state preserved across a compress-mode compaction."""

    todo_snapshot: TodoListSnapshot | None = Field(default=None)
    loaded_skill_names: tuple[str, ...] = Field(default_factory=tuple)
    recent_tool_observations: tuple[str, ...] = Field(default_factory=tuple)
    pending_command_approvals: tuple[str, ...] = Field(default_factory=tuple)
    pending_outbound_drafts: tuple[str, ...] = Field(default_factory=tuple)

    def is_empty(self) -> bool:
        if self.todo_snapshot is not None and self.todo_snapshot.items:
            return False
        return not (
            self.loaded_skill_names
            or self.recent_tool_observations
            or self.pending_command_approvals
            or self.pending_outbound_drafts
        )


def render_rehydration_text(bundle: RehydrationBundle) -> str | None:
    if bundle.is_empty():
        return None

    sections: list[str] = [_REHYDRATION_HEADER]

    if bundle.todo_snapshot is not None and bundle.todo_snapshot.items:
        sections.append("Active plan:")
        if bundle.todo_snapshot.explanation:
            sections.append(f"  note: {bundle.todo_snapshot.explanation}")
        for item in bundle.todo_snapshot.items:
            sections.append(f"  - [{item.status}] {item.id}: {item.title}")

    if bundle.loaded_skill_names:
        sections.append(
            "Skills already loaded this session (do not re-read SKILL.md unnecessarily): "
            + ", ".join(bundle.loaded_skill_names)
        )

    if bundle.pending_command_approvals:
        sections.append(
            "Pending command approvals (operator decision required): " + ", ".join(bundle.pending_command_approvals)
        )

    if bundle.pending_outbound_drafts:
        sections.append(
            "Pending outbound drafts (operator commit required): " + ", ".join(bundle.pending_outbound_drafts)
        )

    if bundle.recent_tool_observations:
        sections.append("Most recent tool observations (oldest first):")
        for observation in bundle.recent_tool_observations:
            sections.append(f"  - {observation}")

    return "\n".join(sections)


def summarize_tool_observation(payload: Any, *, max_chars: int = 500) -> str:
    """Cap an observation payload string so the bundle stays compact."""
    text = str(payload)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 12] + " …truncated"
