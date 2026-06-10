"""
Runtime command handling for the application loop.

`RuntimeCommandHandler` owns the built-in channel commands (/clear, /usage,
/summarize, /yes, /no, /drafts) and their rendering helpers, which used to live
on `ApplicationLoop`. The loop owns one handler instance and delegates to it.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from app.core.config import settings
from app.core.log import logger
from app.core.telemetry import runtime_telemetry
from app.runtime.approvals import approval_store, find_draft_by_id, outbound_draft_store
from app.runtime.channels import ChannelPlugin
from app.runtime.command_execution import run_shell_command
from app.runtime.outbound_dispatch import dispatch_outbound_draft
from app.runtime.session_mode import clear_session_mode
from app.schema.control import ApprovalRequest, OutboundDraft
from app.schema.messages import InboundBatch
from app.util.clock import utcnow

if TYPE_CHECKING:
    from app.runtime.loop import ApplicationLoop

_SUMMARIZE_PROMPT_NAME = "summarize.prompt.md"


class RuntimeCommandHandler:
    def __init__(self, loop: ApplicationLoop) -> None:
        self._loop = loop

    async def handle_command(self, batch: InboundBatch, channel: ChannelPlugin) -> bool:
        parsed = self.recognized_command(batch.raw_text)
        if parsed is None:
            return False

        command, argument = parsed

        if command == "/clear":
            self._loop._sessions[batch.session_key] = await self._loop._chat_service.reset_session(batch.session_key)
            clear_session_mode(batch.session_key)
            response_sent = await self._loop._send_inbound_response(
                channel=channel,
                inbound_message=batch.last_message,
                response_text="Session cleared. Started a new chat session.",
            )
            logger.info(f"Cleared session history for {batch.session_key}")
            if response_sent:
                self._loop._session_telemetry.record_command_response(batch, command)
            else:
                self._loop._session_telemetry.record_command_invocation(batch, command)
                self._loop._session_telemetry.record_session_activity(batch.session_key)
            await runtime_telemetry.record_event(
                event_type="session.command.clear",
                source="application-loop",
                message="Session cleared through runtime command.",
                data={"session_key": batch.session_key, "channel": batch.channel_name},
            )
            return True

        if command == "/yes":
            await self._handle_yes(batch, channel, argument)
            return True

        if command == "/no":
            await self._handle_no(batch, channel, argument)
            return True

        if command == "/drafts":
            await self._handle_drafts_list(batch, channel)
            return True

        session = await self._loop._get_session(batch.session_key)

        if command == "/usage":
            response_sent = await self._loop._send_inbound_response(
                channel=channel,
                inbound_message=batch.last_message,
                response_text=session.render_usage_report(),
            )
            logger.info(f"Reported session token usage for {batch.session_key}")
            if response_sent:
                self._loop._session_telemetry.record_command_response(batch, command)
            else:
                self._loop._session_telemetry.record_command_invocation(batch, command)
                self._loop._session_telemetry.record_session_activity(batch.session_key)
            await runtime_telemetry.record_event(
                event_type="session.command.usage",
                source="application-loop",
                message="Usage report returned through runtime command.",
                data={"session_key": batch.session_key, "channel": batch.channel_name},
            )
            return True

        if command == "/summarize":
            try:
                summarize_prompt = self._loop._chat_service.render_prompt_text(_SUMMARIZE_PROMPT_NAME)
            except Exception:
                logger.exception(f"Failed to load summarize prompt for {batch.session_key}")
                await self._loop._send_inbound_response(
                    channel=channel,
                    inbound_message=batch.last_message,
                    response_text="I could not load the summarize prompt right now. Please try again.",
                )
                return True

            logger.info(f"Running summarize prompt for {batch.session_key}")
            self._loop._session_telemetry.record_command_invocation(batch, command)
            await self._loop._send_session_response(
                channel=channel,
                batch=batch,
                session=session,
                model_input=summarize_prompt,
            )
            return True

        return False

    async def _handle_yes(self, batch: InboundBatch, channel: ChannelPlugin, draft_id: str) -> bool:
        """P1 #21: operator approves and dispatches a pending draft in one step."""
        if not draft_id:
            await self._loop._send_inbound_response(
                channel=channel,
                inbound_message=batch.last_message,
                response_text="Usage: /yes <draft_id>",
            )
            self._loop._session_telemetry.record_command_response(batch, "/yes")
            return False

        located = await find_draft_by_id(draft_id)
        if located is None:
            await self._loop._send_inbound_response(
                channel=channel,
                inbound_message=batch.last_message,
                response_text=f"draft not found: {draft_id}",
            )
            self._loop._session_telemetry.record_command_response(batch, "/yes")
            return False

        kind, record = located
        if record.status != "pending":
            await self._loop._send_inbound_response(
                channel=channel,
                inbound_message=batch.last_message,
                response_text=f"draft {draft_id} is already {record.status}; nothing to do",
            )
            self._loop._session_telemetry.record_command_response(batch, "/yes")
            return False

        if kind == "command":
            await self._yes_command_draft(batch, channel, record)  # type: ignore[arg-type]
        else:
            await self._yes_outbound_draft(batch, channel, record)  # type: ignore[arg-type]
        self._loop._session_telemetry.record_command_response(batch, "/yes")
        return True

    async def _yes_command_draft(
        self,
        batch: InboundBatch,
        channel: ChannelPlugin,
        record: ApprovalRequest,
    ) -> None:
        decided_by = f"channel:{batch.channel_name}"
        try:
            await approval_store.approve(record.id, decided_by=decided_by, comment="approved via /yes")
            approved = await approval_store.consume(record.id)
        except Exception:
            logger.exception(f"Failed to mark command draft {record.id} approved+used via /yes")
            await self._loop._send_inbound_response(
                channel=channel,
                inbound_message=batch.last_message,
                response_text=f"could not approve draft {record.id}: internal error",
            )
            return

        timeout_seconds = (
            approved.timeout_seconds
            if approved.timeout_seconds is not None
            else settings.MCP_DEFAULT_COMMAND_TIMEOUT_SECONDS
        )

        try:
            result = await run_shell_command(
                approved.command,
                directory=approved.directory,
                timeout_seconds=timeout_seconds,
                ctx=None,
            )
        except Exception:
            logger.exception(f"Subprocess for /yes-approved draft {record.id} crashed")
            await self._loop._send_inbound_response(
                channel=channel,
                inbound_message=batch.last_message,
                response_text=f"about to run: `{approved.command}` — crashed before producing output",
            )
            return

        if isinstance(result, dict) and result.get("status") == "error":
            await self._loop._send_inbound_response(
                channel=channel,
                inbound_message=batch.last_message,
                response_text=(
                    f"about to run: `{approved.command}`\n"
                    f"failed: {result.get('type')} {result.get('message') or ''}".rstrip()
                ),
            )
            await runtime_telemetry.record_event(
                event_type="control.approval_used",
                source="channel-command",
                level="warning",
                message="executed_failed",
                data={
                    "draft_id": record.id,
                    "decided_by": decided_by,
                    "command": approved.command,
                    "error_type": result.get("type"),
                },
            )
            return

        exit_code = result.get("exit_code")
        combined_output, _ = self._truncate_for_channel_reply(result.get("combined_output") or "")
        reply = (
            f"about to run: `{approved.command}`, exit_code={exit_code}\n{combined_output}"
            if combined_output
            else f"about to run: `{approved.command}`, exit_code={exit_code}"
        )
        await self._loop._send_inbound_response(
            channel=channel,
            inbound_message=batch.last_message,
            response_text=reply,
        )
        await runtime_telemetry.record_event(
            event_type="control.approval_used",
            source="channel-command",
            level="info",
            message="executed",
            data={
                "draft_id": record.id,
                "decided_by": decided_by,
                "command": approved.command,
                "exit_code": exit_code,
            },
        )

    async def _yes_outbound_draft(
        self,
        batch: InboundBatch,
        channel: ChannelPlugin,
        record: OutboundDraft,
    ) -> None:
        decided_by = f"channel:{batch.channel_name}"
        try:
            committed = await outbound_draft_store.commit(
                record.id,
                decided_by=decided_by,
                comment="committed via /yes",
            )
        except Exception:
            logger.exception(f"Failed to commit outbound draft {record.id} via /yes")
            await self._loop._send_inbound_response(
                channel=channel,
                inbound_message=batch.last_message,
                response_text=f"could not commit draft {record.id}: internal error",
            )
            return

        try:
            dispatch_result = await dispatch_outbound_draft(committed, ctx=None)
        except Exception:
            logger.exception(f"Dispatch crashed for outbound draft {record.id} via /yes")
            await self._loop._send_inbound_response(
                channel=channel,
                inbound_message=batch.last_message,
                response_text=(
                    f"about to send: {committed.kind.value} → {committed.channel}:{committed.target} — dispatch crashed"
                ),
            )
            return

        kind_label = committed.kind.value
        target_label = f"{committed.channel}:{committed.target}" if committed.target else committed.channel
        if isinstance(dispatch_result, dict) and dispatch_result.get("status") == "error":
            await self._loop._send_inbound_response(
                channel=channel,
                inbound_message=batch.last_message,
                response_text=(
                    f"about to send: {kind_label} → {target_label} — "
                    f"dispatch failed: {dispatch_result.get('type')} {dispatch_result.get('message') or ''}".rstrip()
                ),
            )
            await runtime_telemetry.record_event(
                event_type="control.draft_dispatched",
                source="channel-command",
                level="warning",
                message="dispatch_failed",
                data={
                    "draft_id": record.id,
                    "decided_by": decided_by,
                    "kind": kind_label,
                    "channel": committed.channel,
                    "target": committed.target,
                    "error_type": dispatch_result.get("type"),
                },
            )
            return

        await self._loop._send_inbound_response(
            channel=channel,
            inbound_message=batch.last_message,
            response_text=f"dispatch ok: {kind_label} → {target_label}",
        )
        await runtime_telemetry.record_event(
            event_type="control.draft_dispatched",
            source="channel-command",
            level="info",
            message="dispatched",
            data={
                "draft_id": record.id,
                "decided_by": decided_by,
                "kind": kind_label,
                "channel": committed.channel,
                "target": committed.target,
            },
        )

    async def _handle_no(self, batch: InboundBatch, channel: ChannelPlugin, draft_id: str) -> bool:
        """P1 #21: operator denies or discards a pending draft."""
        if not draft_id:
            await self._loop._send_inbound_response(
                channel=channel,
                inbound_message=batch.last_message,
                response_text="Usage: /no <draft_id>",
            )
            self._loop._session_telemetry.record_command_response(batch, "/no")
            return False

        located = await find_draft_by_id(draft_id)
        if located is None:
            await self._loop._send_inbound_response(
                channel=channel,
                inbound_message=batch.last_message,
                response_text=f"draft not found: {draft_id}",
            )
            self._loop._session_telemetry.record_command_response(batch, "/no")
            return False

        kind, record = located
        if record.status != "pending":
            await self._loop._send_inbound_response(
                channel=channel,
                inbound_message=batch.last_message,
                response_text=f"draft {draft_id} is already {record.status}; nothing to do",
            )
            self._loop._session_telemetry.record_command_response(batch, "/no")
            return False

        decided_by = f"channel:{batch.channel_name}"
        try:
            if kind == "command":
                await approval_store.deny(record.id, decided_by=decided_by, comment="denied via /no")
                reply = f"denied: {record.id}"
                event_type = "control.approval_denied"
            else:
                await outbound_draft_store.discard(record.id, decided_by=decided_by, comment="discarded via /no")
                reply = f"discarded: {record.id}"
                event_type = "control.draft_discarded"
        except Exception:
            logger.exception(f"Failed to deny/discard draft {record.id} via /no")
            await self._loop._send_inbound_response(
                channel=channel,
                inbound_message=batch.last_message,
                response_text=f"could not deny draft {record.id}: internal error",
            )
            self._loop._session_telemetry.record_command_response(batch, "/no")
            return False

        await self._loop._send_inbound_response(
            channel=channel,
            inbound_message=batch.last_message,
            response_text=reply,
        )
        await runtime_telemetry.record_event(
            event_type=event_type,
            source="channel-command",
            level="info",
            message="declined",
            data={"draft_id": record.id, "decided_by": decided_by},
        )
        self._loop._session_telemetry.record_command_response(batch, "/no")
        return True

    async def _handle_drafts_list(self, batch: InboundBatch, channel: ChannelPlugin) -> None:
        """P1 #21: print the pending drafts of both kinds, oldest first, capped at 10."""
        try:
            command_drafts = await approval_store.list(status="pending")
        except Exception:
            logger.exception("Failed to list pending command drafts for /drafts")
            command_drafts = []
        try:
            outbound_drafts = await outbound_draft_store.list(status="pending")
        except Exception:
            logger.exception("Failed to list pending outbound drafts for /drafts")
            outbound_drafts = []

        entries: list[tuple[datetime, str]] = []
        for record in command_drafts:
            preview = record.command.replace("\n", " ")
            entries.append((record.requested_at, self._render_draft_line("command", record.id, record.source, preview)))
        for record in outbound_drafts:
            preview_source = record.message or (record.attachment.path if record.attachment is not None else "")
            preview = f"{record.kind.value} → {record.channel}:{record.target} {preview_source}".strip()
            entries.append(
                (record.requested_at, self._render_draft_line("outbound", record.id, record.source, preview))
            )

        entries.sort(key=lambda entry: entry[0])
        capped_entries = entries[:10]

        if not capped_entries:
            reply_text = "no pending drafts"
        else:
            now = utcnow()
            rendered_lines = [
                self._format_draft_line_with_age(line, requested_at, now) for requested_at, line in capped_entries
            ]
            header = f"pending drafts ({len(capped_entries)}{'+' if len(entries) > 10 else ''}):"
            reply_text = header + "\n" + "\n".join(rendered_lines)

        await self._loop._send_inbound_response(
            channel=channel,
            inbound_message=batch.last_message,
            response_text=reply_text,
        )
        self._loop._session_telemetry.record_command_response(batch, "/drafts")

    @staticmethod
    def _render_draft_line(kind: str, draft_id: str, source: str, preview: str) -> str:
        truncated_preview = preview if len(preview) <= 80 else preview[:77] + "..."
        return f"{kind} {draft_id} src={source} | {truncated_preview}"

    @staticmethod
    def _format_draft_line_with_age(line: str, requested_at: datetime, now: datetime) -> str:
        age_seconds = max(int((now - requested_at).total_seconds()), 0)
        if age_seconds < 60:
            age = f"{age_seconds}s"
        elif age_seconds < 3600:
            age = f"{age_seconds // 60}m"
        else:
            age = f"{age_seconds // 3600}h"
        return f"{line} ({age} ago)"

    @staticmethod
    def _truncate_for_channel_reply(text: str, *, max_chars: int = 1500) -> tuple[str, bool]:
        if len(text) <= max_chars:
            return text, False
        return text[:max_chars] + "\n…[truncated]", True

    def recognized_command(self, raw_text: str) -> tuple[str, str] | None:
        stripped_text = raw_text.strip()
        if not stripped_text.startswith("/"):
            return None

        head, _, rest = stripped_text.partition(" ")
        command = head.lower()
        argument = rest.strip()

        if command in {"/clear", "/summarize", "/usage", "/drafts"}:
            if argument:
                return None
            return command, ""

        if command in {"/yes", "/no"}:
            return command, argument

        return None
