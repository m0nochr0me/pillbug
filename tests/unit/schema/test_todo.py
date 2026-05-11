"""TodoList invariants — single in-progress, unique ids, title normalization."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schema.todo import TodoItem, TodoListSnapshot


def _item(item_id: int, status: str = "not-started", title: str = "task") -> TodoItem:
    return TodoItem(id=item_id, status=status, title=title)


class TestTodoItem:
    def test_title_whitespace_is_collapsed(self):
        item = TodoItem(id=1, status="not-started", title="  one   two\tthree  ")
        assert item.title == "one two three"

    def test_blank_title_is_rejected(self):
        with pytest.raises(ValidationError):
            TodoItem(id=1, status="not-started", title="   ")

    def test_id_must_be_positive(self):
        with pytest.raises(ValidationError):
            TodoItem(id=0, status="not-started", title="task")


class TestTodoListSnapshot:
    def test_duplicate_ids_are_rejected(self):
        with pytest.raises(ValidationError):
            TodoListSnapshot(items=[_item(1), _item(1, title="dup")])

    def test_multiple_in_progress_are_rejected(self):
        with pytest.raises(ValidationError):
            TodoListSnapshot(
                items=[
                    _item(1, status="in-progress"),
                    _item(2, status="in-progress"),
                ]
            )

    def test_single_in_progress_is_allowed(self):
        snapshot = TodoListSnapshot(
            items=[
                _item(1, status="completed"),
                _item(2, status="in-progress"),
                _item(3, status="not-started"),
            ]
        )
        assert snapshot.counts == {"not-started": 1, "in-progress": 1, "completed": 1}
