"""
Schema definitions for session-scoped todo planning.
"""

from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TodoItem(BaseModel):
    id: int = Field(ge=1)
    title: str = Field(min_length=1)
    status: Literal["not-started", "in-progress", "completed"]

    @model_validator(mode="after")
    def normalize_title(self) -> Self:
        self.title = " ".join(self.title.split())
        if not self.title:
            raise ValueError("title must not be empty")

        return self


class TodoListSnapshot(BaseModel):
    items: list[TodoItem] = Field(default_factory=list)
    explanation: str | None = None
    updated_at: datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def validate_items(self) -> Self:
        seen_ids: set[int] = set()
        in_progress_count = 0

        for item in self.items:
            if item.id in seen_ids:
                raise ValueError(f"Duplicate todo item id: {item.id}")

            seen_ids.add(item.id)
            if item.status == "in-progress":
                in_progress_count += 1

        if in_progress_count > 1:
            raise ValueError("Only one todo item may be in-progress at a time")

        if self.explanation is not None:
            self.explanation = " ".join(self.explanation.split()) or None

        return self

    @property
    def counts(self) -> dict[str, int]:
        return {
            "not-started": sum(item.status == "not-started" for item in self.items),
            "in-progress": sum(item.status == "in-progress" for item in self.items),
            "completed": sum(item.status == "completed" for item in self.items),
        }
