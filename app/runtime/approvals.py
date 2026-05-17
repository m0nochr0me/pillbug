"""
Persistent draft → approval state for high-risk MCP tool calls (plan P0 #2).

The model proposes a command via `draft_command`, the operator approves or denies
through the control API, and the model then redeems the approval through
`run_approved_command`. Drafts are single-shot — once redeemed, the record moves
to `used` and any later redeem returns `already_used`.

Persistence lives under `{settings.BASE_DIR}/approvals/<id>.json`; the in-memory
cache rebuilds from disk on first access and whenever `settings.BASE_DIR` changes
(tests monkeypatch BASE_DIR per case, so we re-resolve on every operation).
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from app.core.config import settings
from app.core.log import logger
from app.schema.control import (
    ApprovalRequest,
    ApprovalStatus,
    OutboundAttachmentDraft,
    OutboundDraft,
    OutboundDraftKind,
    OutboundDraftStatus,
)

__all__ = (
    "ApprovalStore",
    "DraftKind",
    "OutboundDraftStore",
    "approval_store",
    "find_draft_by_id",
    "outbound_draft_store",
)


DraftKind = Literal["command", "outbound"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ApprovalStore:
    """In-memory cache plus filesystem persistence for command drafts."""

    def __init__(self, subdirectory: str = "approvals") -> None:
        self._subdirectory = subdirectory
        self._lock = asyncio.Lock()
        self._cache: dict[str, ApprovalRequest] = {}
        self._loaded_dir: Path | None = None

    @property
    def base_dir(self) -> Path:
        return settings.BASE_DIR / self._subdirectory

    def _file_path(self, draft_id: str) -> Path:
        return self.base_dir / f"{draft_id}.json"

    async def _ensure_loaded(self) -> None:
        current = self.base_dir
        if self._loaded_dir == current:
            return
        async with self._lock:
            if self._loaded_dir == current:
                return
            await asyncio.to_thread(current.mkdir, parents=True, exist_ok=True)
            self._cache.clear()
            for path in sorted(await asyncio.to_thread(lambda: list(current.glob("*.json")))):
                try:
                    raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
                    record = ApprovalRequest.model_validate_json(raw)
                    self._cache[record.id] = record
                except Exception as exc:
                    logger.warning(f"Failed to load approval record {path.name}: {exc}")
            self._loaded_dir = current

    async def _persist(self, record: ApprovalRequest) -> None:
        path = self._file_path(record.id)
        payload = record.model_dump_json(indent=2)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_text, payload, encoding="utf-8")

    async def create_draft(
        self,
        *,
        command: str,
        justification: str,
        source: str,
        directory: str = ".",
        timeout_seconds: float | None = None,
    ) -> ApprovalRequest:
        await self._ensure_loaded()
        draft_id = secrets.token_urlsafe(12)
        record = ApprovalRequest(
            id=draft_id,
            command=command,
            justification=justification,
            directory=directory,
            timeout_seconds=timeout_seconds,
            status="pending",
            source=source,
            requested_at=_utcnow(),
        )
        async with self._lock:
            self._cache[draft_id] = record
            await self._persist(record)
        return record

    async def get(self, draft_id: str) -> ApprovalRequest | None:
        await self._ensure_loaded()
        return self._cache.get(draft_id)

    async def list(self, *, status: ApprovalStatus | None = None) -> list[ApprovalRequest]:
        await self._ensure_loaded()
        records = list(self._cache.values())
        if status is not None:
            records = [r for r in records if r.status == status]
        records.sort(key=lambda r: r.requested_at, reverse=True)
        return records

    async def approve(
        self,
        draft_id: str,
        *,
        decided_by: str,
        comment: str | None = None,
    ) -> ApprovalRequest:
        return await self._decide(draft_id, "approved", decided_by=decided_by, comment=comment)

    async def deny(
        self,
        draft_id: str,
        *,
        decided_by: str,
        comment: str | None = None,
    ) -> ApprovalRequest:
        return await self._decide(draft_id, "denied", decided_by=decided_by, comment=comment)

    async def _decide(
        self,
        draft_id: str,
        new_status: ApprovalStatus,
        *,
        decided_by: str,
        comment: str | None,
    ) -> ApprovalRequest:
        await self._ensure_loaded()
        async with self._lock:
            record = self._cache.get(draft_id)
            if record is None:
                raise LookupError(f"Approval draft not found: {draft_id}")
            if record.status != "pending":
                raise PermissionError(f"Approval draft is already {record.status}; cannot transition to {new_status}")
            updated = record.model_copy(
                update={
                    "status": new_status,
                    "decided_at": _utcnow(),
                    "decided_by": decided_by,
                    "decided_comment": comment,
                }
            )
            self._cache[draft_id] = updated
            await self._persist(updated)
        return updated

    async def consume(self, draft_id: str) -> ApprovalRequest:
        await self._ensure_loaded()
        async with self._lock:
            record = self._cache.get(draft_id)
            if record is None:
                raise LookupError(f"Approval draft not found: {draft_id}")
            if record.status == "used":
                raise PermissionError(f"Approval draft already used: {draft_id}")
            if record.status != "approved":
                raise PermissionError(f"Approval draft is not approved (status={record.status}): {draft_id}")
            updated = record.model_copy(update={"status": "used", "used_at": _utcnow()})
            self._cache[draft_id] = updated
            await self._persist(updated)
        return updated


approval_store = ApprovalStore()


class OutboundDraftStore:
    """In-memory cache plus filesystem persistence for outbound-send drafts (plan P0 #3)."""

    def __init__(self, subdirectory: str = "drafts") -> None:
        self._subdirectory = subdirectory
        self._lock = asyncio.Lock()
        self._cache: dict[str, OutboundDraft] = {}
        self._loaded_dir: Path | None = None

    @property
    def base_dir(self) -> Path:
        return settings.BASE_DIR / self._subdirectory

    def _file_path(self, draft_id: str) -> Path:
        return self.base_dir / f"{draft_id}.json"

    async def _ensure_loaded(self) -> None:
        current = self.base_dir
        if self._loaded_dir == current:
            return
        async with self._lock:
            if self._loaded_dir == current:
                return
            await asyncio.to_thread(current.mkdir, parents=True, exist_ok=True)
            self._cache.clear()
            for path in sorted(await asyncio.to_thread(lambda: list(current.glob("*.json")))):
                try:
                    raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
                    record = OutboundDraft.model_validate_json(raw)
                    self._cache[record.id] = record
                except Exception as exc:
                    logger.warning(f"Failed to load outbound draft {path.name}: {exc}")
            self._loaded_dir = current

    async def _persist(self, record: OutboundDraft) -> None:
        path = self._file_path(record.id)
        payload = record.model_dump_json(indent=2)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_text, payload, encoding="utf-8")

    async def create(
        self,
        *,
        kind: OutboundDraftKind,
        channel: str,
        target: str,
        message: str,
        source: str,
        attachment: OutboundAttachmentDraft | None = None,
        timeout_seconds: float | None = None,
    ) -> OutboundDraft:
        await self._ensure_loaded()
        draft_id = secrets.token_urlsafe(12)
        record = OutboundDraft(
            id=draft_id,
            kind=kind,
            channel=channel,
            target=target,
            message=message,
            attachment=attachment,
            timeout_seconds=timeout_seconds,
            source=source,
            requested_at=_utcnow(),
        )
        async with self._lock:
            self._cache[draft_id] = record
            await self._persist(record)
        return record

    async def get(self, draft_id: str) -> OutboundDraft | None:
        await self._ensure_loaded()
        return self._cache.get(draft_id)

    async def list(self, *, status: OutboundDraftStatus | None = None) -> list[OutboundDraft]:
        await self._ensure_loaded()
        records = list(self._cache.values())
        if status is not None:
            records = [r for r in records if r.status == status]
        records.sort(key=lambda r: r.requested_at, reverse=True)
        return records

    async def commit(
        self,
        draft_id: str,
        *,
        decided_by: str,
        comment: str | None = None,
    ) -> OutboundDraft:
        return await self._transition(draft_id, "committed", decided_by=decided_by, comment=comment)

    async def discard(
        self,
        draft_id: str,
        *,
        decided_by: str,
        comment: str | None = None,
    ) -> OutboundDraft:
        return await self._transition(draft_id, "discarded", decided_by=decided_by, comment=comment)

    async def _transition(
        self,
        draft_id: str,
        new_status: OutboundDraftStatus,
        *,
        decided_by: str,
        comment: str | None,
    ) -> OutboundDraft:
        await self._ensure_loaded()
        async with self._lock:
            record = self._cache.get(draft_id)
            if record is None:
                raise LookupError(f"Outbound draft not found: {draft_id}")
            if record.status != "pending":
                raise PermissionError(f"Outbound draft is already {record.status}; cannot transition to {new_status}")
            updated = record.model_copy(
                update={
                    "status": new_status,
                    "decided_at": _utcnow(),
                    "decided_by": decided_by,
                    "decided_comment": comment,
                }
            )
            self._cache[draft_id] = updated
            await self._persist(updated)
        return updated


outbound_draft_store = OutboundDraftStore()


async def find_draft_by_id(draft_id: str) -> tuple[DraftKind, ApprovalRequest | OutboundDraft] | None:
    """Look up a draft id across both stores; returns (kind, record) or None.

    Used by the runtime /yes /no /drafts commands (plan P1 #21) so the operator
    can decide on either a command-approval draft or an outbound-send draft with
    the same verb.
    """
    command_record = await approval_store.get(draft_id)
    if command_record is not None:
        return ("command", command_record)
    outbound_record = await outbound_draft_store.get(draft_id)
    if outbound_record is not None:
        return ("outbound", outbound_record)
    return None
