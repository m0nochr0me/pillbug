"""
AI client
"""

import asyncio
import mimetypes
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiofile
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import ValidationError

from app.core.config import settings
from app.core.jinja import render_template
from app.core.log import logger
from app.runtime.channels import get_available_channels_context, get_channel_plugin
from app.runtime.session_binding import (
    bind_mcp_session_to_runtime_session,
    bind_runtime_session_todo_snapshot,
    consume_pending_outbound_injections,
    get_runtime_session_todo_snapshot,
)
from app.schema.ai import ChatResponse, ChatSessionSnapshot, ChatSessionUsageTotals, InboundAttachment, Skill
from app.schema.messages import extract_a2a_origin_route
from app.schema.todo import TodoListSnapshot
from app.util.base_dir import get_module_root
from app.util.skills import discover_workspace_skills
from app.util.workspace import resolve_path_within_root

__all__ = (
    "GeminiChatService",
    "GeminiChatSession",
    "chat_service",
)

_TEXT_ATTACHMENT_MIME_TYPES = {
    "text/markdown": "text/markdown",
    "text/plain": "text/plain",
    "text/x-markdown": "text/markdown",
}
_ATTACHMENT_MIME_TYPE_OVERRIDES = {
    ".markdown": "text/markdown",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
}
_INLINE_ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024
_COMPRESSED_SESSION_HISTORY_PROMPT_NAME = "compressed_session_history.prompt.md"
_DIRECT_REPLY_CHANNEL_MEMO_PROMPT_NAME = "direct_reply_channel_memo.prompt.md"
_EMPTY_MODEL_RESPONSE_FALLBACK_PROMPT_NAME = "empty_model_response_fallback.prompt.md"
_EMPTY_RESPONSE_NUDGE_PROMPT_NAME = "empty_response_nudge.prompt.md"
_MODEL_INPUT_PROMPT_NAME = "model_input.prompt.md"
_SKILLS_PROMPT_NAME = "skills.prompt.md"
_CHANNEL_MEMO_PROMPTS = {"a2a": "a2a_channel_memo.prompt.md", "telegram": "telegram_channel_memo.prompt.md"}
_DIRECT_REPLY_CHANNEL_EXCLUSIONS = frozenset({"a2a", "trigger"})
_TODO_STATUS_LABELS = {
    "not-started": "not started",
    "in-progress": "in progress",
    "completed": "completed",
}


def _has_file_data_parts(history: list[types.Content]) -> bool:
    for content in history:
        for part in content.parts or []:
            if getattr(part, "file_data", None) is not None:
                return True
    return False


def _strip_file_data_parts(history: list[types.Content]) -> list[types.Content]:
    sanitized: list[types.Content] = []
    for content in history:
        parts = content.parts or []
        kept = [part for part in parts if getattr(part, "file_data", None) is None]
        if not kept:
            continue
        sanitized.append(types.Content(role=content.role, parts=kept))
    return sanitized


def _extract_injectable_content(history: list[types.Content]) -> types.Content | None:
    """Return the last model response from history with only text and thought parts (thought_signature preserved)."""
    for content in reversed(history):
        if getattr(content, "role", None) != "model":
            continue
        injectable_parts = [
            p
            for p in (content.parts or [])
            if getattr(p, "thought", False) or isinstance(getattr(p, "text", None), str)
        ]
        if injectable_parts:
            return types.Content(role="model", parts=injectable_parts)
    return None


def _normalize_supported_attachment_mime_type(mime_type: str) -> str | None:
    normalized_mime_type = mime_type.strip().lower()
    if not normalized_mime_type:
        return None
    if normalized_mime_type.startswith("audio/"):
        return normalized_mime_type
    if normalized_mime_type.startswith("image/"):
        return normalized_mime_type
    if normalized_mime_type == "application/pdf":
        return normalized_mime_type
    return _TEXT_ATTACHMENT_MIME_TYPES.get(normalized_mime_type)


def _supported_attachment_mime_type(attachment_path: Path, attachment: InboundAttachment) -> str | None:
    candidates: list[str] = []

    if attachment.mime_type:
        candidates.append(attachment.mime_type)

    suffix_override = _ATTACHMENT_MIME_TYPE_OVERRIDES.get(attachment_path.suffix.lower())
    if suffix_override is not None:
        candidates.append(suffix_override)

    guessed_mime_type, _ = mimetypes.guess_type(attachment_path.name)
    if guessed_mime_type is not None:
        candidates.append(guessed_mime_type)

    if attachment.kind == "photo":
        candidates.append("image/jpeg")

    for candidate in candidates:
        if normalized_candidate := _normalize_supported_attachment_mime_type(candidate):
            return normalized_candidate

    return None


def _legacy_attachment_from_metadata(metadata: dict[str, Any]) -> InboundAttachment | None:
    attachment_path = metadata.get("telegram_attachment_download_path")
    if not isinstance(attachment_path, str) or not attachment_path.strip():
        return None

    return InboundAttachment(
        path=attachment_path,
        mime_type=metadata.get("telegram_attachment_mime_type")
        if isinstance(metadata.get("telegram_attachment_mime_type"), str)
        else None,
        display_name=(
            metadata.get("telegram_attachment_original_file_name")
            if isinstance(metadata.get("telegram_attachment_original_file_name"), str)
            else None
        ),
        source="telegram",
        kind=metadata.get("telegram_attachment_type")
        if isinstance(metadata.get("telegram_attachment_type"), str)
        else None,
    )


def _extract_inbound_attachments(metadata: dict[str, Any]) -> list[InboundAttachment]:
    attachments: list[InboundAttachment] = []
    raw_attachments = metadata.get("inbound_attachments")

    raw_values: list[object] = []
    if isinstance(raw_attachments, list | tuple):
        raw_values.extend(raw_attachments)
    elif isinstance(raw_attachments, dict):
        raw_values.append(raw_attachments)

    for raw_value in raw_values:
        try:
            attachments.append(InboundAttachment.model_validate(raw_value))
        except ValidationError as exc:
            logger.warning(f"Skipping invalid inbound attachment metadata entry: {exc}")

    if not attachments and (legacy_attachment := _legacy_attachment_from_metadata(metadata)) is not None:
        attachments.append(legacy_attachment)

    return attachments


def _normalize_channel_name(channel_name: str | None) -> str | None:
    if channel_name is None:
        return None

    normalized_channel_name = channel_name.strip().lower()
    return normalized_channel_name or None


def _filter_base_context_channels(channels: list[str]) -> list[str]:
    return [channel for channel in channels if channel.partition(":")[0].strip().lower() != "trigger"]


def _resolve_user_origin_channel(
    channel_name: str | None,
    message_metadata: list[dict[str, Any]] | None,
) -> str | None:
    if message_metadata:
        for metadata in reversed(message_metadata):
            if origin_route := extract_a2a_origin_route(metadata):
                return _normalize_channel_name(origin_route[0])

    return _normalize_channel_name(channel_name)


def _resolve_direct_reply_channel_name(
    channel_name: str | None,
    message_metadata: list[dict[str, Any]] | None,
) -> str | None:
    origin_channel_name = _resolve_user_origin_channel(channel_name, message_metadata)
    if origin_channel_name is None or origin_channel_name in _DIRECT_REPLY_CHANNEL_EXCLUSIONS:
        return None

    return origin_channel_name


def _render_todo_list_instruction(todo_snapshot: TodoListSnapshot | None) -> str | None:
    if todo_snapshot is None or not todo_snapshot.items:
        return None

    lines = [
        "Current session todo list:",
    ]

    if todo_snapshot.explanation:
        lines.append(f"Plan note: {todo_snapshot.explanation}")

    for item in todo_snapshot.items:
        lines.append(f"{item.id}. [{_TODO_STATUS_LABELS[item.status]}] {item.title}")  # noqa: PERF401

    lines.append("Use manage_todo_list to keep this plan accurate when progress changes.")
    return "\n".join(lines)


class GeminiChatService:
    def __init__(self) -> None:
        self.ai_client = genai.Client(api_key=settings.GEMINI_API_KEY)
        self._sessions_dir = settings.SESSIONS_DIR
        self._module_root = get_module_root("app")
        self._prompts_dir = self._module_root / "prompts"
        self._outbound_injection_handler: Callable[[str, types.Content], Awaitable[None]] | None = None

    def set_outbound_injection_handler(self, handler: Callable[[str, types.Content], Awaitable[None]] | None) -> None:
        self._outbound_injection_handler = handler

    def create_session(
        self,
        session_id: str,
        history: list[types.Content] | None = None,
        usage_totals: ChatSessionUsageTotals | None = None,
    ) -> GeminiChatSession:
        return GeminiChatSession(self, session_id=session_id, history=history, usage_totals=usage_totals)

    async def restore_session(self, session_id: str) -> GeminiChatSession:
        snapshot = await self._load_session_snapshot(session_id)
        history = snapshot.history if snapshot is not None else None
        usage_totals = snapshot.usage_totals if snapshot is not None else None
        if history:
            logger.info(f"Restored session history for {session_id} with {len(history)} messages")

        return self.create_session(
            session_id=session_id,
            history=history or None,
            usage_totals=usage_totals,
        )

    async def reset_session(self, session_id: str) -> GeminiChatSession:
        await self._delete_session_history(session_id)
        bind_runtime_session_todo_snapshot(session_id, None)
        return self.create_session(session_id=session_id)

    async def save_session_history(
        self,
        session_id: str,
        history: list[types.Content],
        usage_totals: ChatSessionUsageTotals,
        system_instruction: str | None = None,
    ) -> None:
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        snapshot = ChatSessionSnapshot(
            session_id=session_id,
            history=history,
            usage_totals=usage_totals,
            system_instruction=system_instruction,
        )
        session_path = self._get_session_path(session_id)

        async with aiofile.AIOFile(session_path, "w", encoding="utf-8") as session_file:
            await session_file.write(snapshot.model_dump_json(indent=2))

    async def _load_session_snapshot(self, session_id: str) -> ChatSessionSnapshot | None:
        session_path = self._get_session_path(session_id)
        if not session_path.is_file():
            return None

        try:
            async with aiofile.AIOFile(session_path, "r", encoding="utf-8") as session_file:
                return ChatSessionSnapshot.model_validate_json(str(await session_file.read()))
        except Exception:
            logger.exception(f"Failed to restore session history from {session_path}")
            return None

    async def _delete_session_history(self, session_id: str) -> None:
        session_path = self._get_session_path(session_id)
        if session_path.exists():
            session_path.unlink()

    def _get_session_path(self, session_id: str):
        return self._sessions_dir / quote(session_id, safe="")

    def _resolve_prompt_path(self, prompt_name: str) -> Path:
        normalized_prompt_name = Path(prompt_name).name
        if normalized_prompt_name != prompt_name:
            raise ValueError(f"Prompt name must not contain directory segments: {prompt_name}")

        prompt_path = self._prompts_dir / normalized_prompt_name
        if not prompt_path.is_file():
            raise FileNotFoundError(f"Prompt file not found: {prompt_path}")

        return prompt_path

    async def read_prompt_text(self, prompt_name: str) -> str:
        return self.render_prompt_text(prompt_name)

    def render_prompt_text(self, prompt_name: str, **context: Any) -> str:
        prompt_path = self._resolve_prompt_path(prompt_name)
        template_name = prompt_path.relative_to(self._module_root).as_posix()
        return render_template(template_name, **context)

    def render_required_prompt_text(self, prompt_name: str, **context: Any) -> str:
        rendered = self.render_prompt_text(prompt_name, **context).strip()
        if not rendered:
            raise ValueError(f"Prompt rendered blank text: {prompt_name}")

        return rendered

    async def get_base_context(
        self,
        *,
        channel_name: str | None = None,
        message_metadata: list[dict[str, Any]] | None = None,
    ) -> str:
        now = datetime.now(ZoneInfo(settings.TIMEZONE))
        available_channels = _filter_base_context_channels(await get_available_channels_context())
        base_context_lines = [
            "---",
            f"datetime: {now:%Y-%b-%d %H:%M:%S}",
            f"timezone: {settings.TIMEZONE}",
            f"workspace: {settings.WORKSPACE_ROOT}",
            f"available_channels: {', '.join(available_channels)}",
        ]

        if direct_reply_channel_name := _resolve_direct_reply_channel_name(channel_name, message_metadata):
            direct_reply_instruction = self.render_prompt_text(
                _DIRECT_REPLY_CHANNEL_MEMO_PROMPT_NAME,
                channel_name=direct_reply_channel_name,
            ).strip()
            if direct_reply_instruction:
                base_context_lines.append(direct_reply_instruction)

        base_context_lines.append("---\n")

        return "\n".join(base_context_lines)

    def _render_channel_instruction_memo(self, channel_name: str, context: dict[str, Any]) -> str | None:
        prompt_name = _CHANNEL_MEMO_PROMPTS.get(channel_name)
        if prompt_name is None:
            return None

        rendered = self.render_prompt_text(prompt_name, **context).strip()
        return rendered or None

    async def get_channel_instruction_memos(self) -> list[str]:
        memos: list[str] = []

        for channel_name in settings.enabled_channels():
            channel = get_channel_plugin(channel_name, create=True)
            if channel is None:
                continue

            instruction_context = getattr(channel, "instruction_context", None)
            if not callable(instruction_context):
                continue

            try:
                context = instruction_context()
            except Exception:
                logger.exception(f"Failed to build instruction context for channel={channel_name}")
                continue

            if not isinstance(context, dict) or not context:
                continue

            if memo := self._render_channel_instruction_memo(channel_name, context):
                memos.append(memo)

        return memos

    async def discover_skills(self) -> list[Skill]:
        """
        Glob directories in the skills base dir, and treat each one as a skill
        """
        return await asyncio.to_thread(discover_workspace_skills, settings.WORKSPACE_ROOT)

    async def build_system_instruction(
        self,
        *,
        session_id: str | None = None,
        channel_name: str | None = None,
        message_metadata: list[dict[str, Any]] | None = None,
    ) -> str | None:
        async with aiofile.AIOFile(settings.WORKSPACE_ROOT / "AGENTS.md", "r", encoding="utf-8") as agents_file:
            agents_md = str(await agents_file.read())
            todo_list = _render_todo_list_instruction(
                get_runtime_session_todo_snapshot(session_id) if session_id is not None else None
            )
            skills_prompt: str | None = None
            if skills := await self.discover_skills():
                skills_prompt = self.render_prompt_text(
                    _SKILLS_PROMPT_NAME,
                    skills=skills,
                )
            channel_memos = await self.get_channel_instruction_memos()
            return self.render_prompt_text(
                _MODEL_INPUT_PROMPT_NAME,
                base_context=await self.get_base_context(
                    channel_name=channel_name,
                    message_metadata=message_metadata,
                ),
                agents_md=agents_md,
                channel_memos=tuple(channel_memos),
                todo_list=todo_list,
                skills=skills_prompt.strip() if skills_prompt else None,
            )


class GeminiChatSession:
    def __init__(
        self,
        service: GeminiChatService,
        session_id: str,
        history: list[types.Content] | None = None,
        usage_totals: ChatSessionUsageTotals | None = None,
    ) -> None:
        self._service = service
        self._session_id = session_id
        self._usage_totals = (
            usage_totals.model_copy(deep=True) if usage_totals is not None else ChatSessionUsageTotals()
        )
        self._latest_system_instruction: str | None = None
        self._mcp_client = Client(
            StreamableHttpTransport(
                f"http://{settings.MCP_HOST}:{settings.MCP_PORT}/mcp",
            )
        )
        self._chat = service.ai_client.aio.chats.create(
            model=settings.GEMINI_MODEL,
            history=cast("list[types.ContentOrDict] | None", history),
        )

    async def send_message(
        self,
        message: str,
        message_metadata: list[dict[str, Any]] | None = None,
        channel_name: str | None = None,
    ) -> ChatResponse:
        async with asyncio.timeout(settings.GEMINI_RESPONSE_TIMEOUT_SECONDS):
            message_parts = await self._build_message_parts(message, message_metadata)
            system_instruction = await self._service.build_system_instruction(
                session_id=self._session_id,
                channel_name=channel_name,
                message_metadata=message_metadata,
            )
            self._latest_system_instruction = system_instruction

            async with self._mcp_client as mcp_client:
                mcp_session_id = mcp_client.transport.get_session_id()
                if mcp_session_id is not None:
                    bind_mcp_session_to_runtime_session(mcp_session_id, self._session_id)

                response = await self._send_chat_message(
                    message=message_parts,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=settings.GEMINI_TEMPERATURE,
                        top_p=settings.GEMINI_TOP_P,
                        max_output_tokens=settings.GEMINI_MAX_OUTPUT_TOKENS,
                        thinking_config=types.ThinkingConfig(
                            thinking_level=types.ThinkingLevel(settings.GEMINI_THINKING_LEVEL)
                        ),
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(
                            maximum_remote_calls=settings.GEMINI_MAX_AFC_CALLS,
                        ),
                        tools=[mcp_client.session],
                    ),
                )

            full_response = response.text or self._extract_parts_text(response.parts)
            self._usage_totals.add_usage_metadata(response.usage_metadata)
            await self._persist_history()
            await self._flush_outbound_injections()
            if full_response.strip():
                return ChatResponse(
                    text=full_response,
                    usage_metadata=response.usage_metadata,
                )

            latest_model_response = self._get_latest_model_response_text()
            if latest_model_response.strip():
                return ChatResponse(
                    text=latest_model_response,
                    usage_metadata=response.usage_metadata,
                )

            max_nudges = settings.GEMINI_EMPTY_RESPONSE_MAX_NUDGES
            for nudge_attempt in range(1, max_nudges + 1):
                logger.info(
                    f"Gemini returned no text for session={self._session_id}; sending nudge {nudge_attempt}/{max_nudges}"
                )
                system_instruction = await self._service.build_system_instruction(
                    session_id=self._session_id,
                    channel_name=channel_name,
                    message_metadata=message_metadata,
                )
                self._latest_system_instruction = system_instruction

                async with self._mcp_client as mcp_client:
                    mcp_session_id = mcp_client.transport.get_session_id()
                    if mcp_session_id is not None:
                        bind_mcp_session_to_runtime_session(mcp_session_id, self._session_id)

                    nudge_response = await self._send_chat_message(
                        message=self._service.render_required_prompt_text(_EMPTY_RESPONSE_NUDGE_PROMPT_NAME),
                        config=types.GenerateContentConfig(
                            system_instruction=system_instruction,
                            temperature=settings.GEMINI_TEMPERATURE,
                            top_p=settings.GEMINI_TOP_P,
                            max_output_tokens=settings.GEMINI_MAX_OUTPUT_TOKENS,
                            thinking_config=types.ThinkingConfig(
                                thinking_level=types.ThinkingLevel(settings.GEMINI_THINKING_LEVEL)
                            ),
                            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                                maximum_remote_calls=settings.GEMINI_MAX_AFC_CALLS,
                            ),
                            tools=[mcp_client.session],
                        ),
                    )

                nudge_text = nudge_response.text or self._extract_parts_text(nudge_response.parts)
                self._usage_totals.add_usage_metadata(nudge_response.usage_metadata)
                await self._persist_history()
                await self._flush_outbound_injections()

                if nudge_text.strip():
                    logger.info(f"Nudge {nudge_attempt} produced text for session={self._session_id}")
                    return ChatResponse(
                        text=nudge_text,
                        usage_metadata=nudge_response.usage_metadata,
                    )

                latest_model_response = self._get_latest_model_response_text()
                if latest_model_response.strip():
                    return ChatResponse(
                        text=latest_model_response,
                        usage_metadata=nudge_response.usage_metadata,
                    )

            logger.warning(
                f"Gemini returned no text for session={self._session_id} after {max_nudges} nudge(s); responding with fallback text instead"
            )

            return ChatResponse(
                text=self._service.render_required_prompt_text(_EMPTY_MODEL_RESPONSE_FALLBACK_PROMPT_NAME),
                usage_metadata=response.usage_metadata,
            )

    async def _build_message_parts(
        self,
        message: str,
        message_metadata: list[dict[str, Any]] | None,
    ) -> list[types.Part]:
        message_parts = [types.Part.from_text(text=message)]
        if not message_metadata:
            return message_parts

        seen_attachment_paths: set[str] = set()
        forwarded_attachment_count = 0
        skipped_attachment_count = 0
        for metadata in message_metadata:
            for attachment in _extract_inbound_attachments(metadata):
                attachment_part = await self._build_attachment_part(attachment, seen_attachment_paths)
                if attachment_part is not None:
                    message_parts.append(attachment_part)
                    forwarded_attachment_count += 1
                else:
                    skipped_attachment_count += 1

        if forwarded_attachment_count or skipped_attachment_count:
            logger.info(
                f"Prepared inbound Gemini attachments for session={self._session_id} forwarded={forwarded_attachment_count} skipped={skipped_attachment_count}"
            )

        return message_parts

    async def _build_attachment_part(
        self,
        attachment: InboundAttachment,
        seen_attachment_paths: set[str],
    ) -> types.Part | None:
        normalized_attachment_path = attachment.path.strip()
        if not normalized_attachment_path or normalized_attachment_path in seen_attachment_paths:
            if normalized_attachment_path:
                logger.info(
                    f"Skipping duplicate inbound attachment for Gemini session={self._session_id} path={normalized_attachment_path}"
                )
            return None

        try:
            attachment_path = resolve_path_within_root(normalized_attachment_path, settings.WORKSPACE_ROOT)
        except ValueError:
            logger.warning(
                f"Skipping inbound attachment outside workspace for Gemini session={self._session_id} path={normalized_attachment_path}"
            )
            return None

        if not await asyncio.to_thread(attachment_path.is_file):
            logger.warning(
                f"Skipping missing inbound attachment for Gemini session={self._session_id} path={normalized_attachment_path}"
            )
            return None

        mime_type = _supported_attachment_mime_type(attachment_path, attachment)
        if mime_type is None:
            logger.info(
                f"Skipping unsupported inbound attachment for Gemini session={self._session_id} path={normalized_attachment_path} kind={attachment.kind} source={attachment.source}"
            )
            return None

        file_size = await asyncio.to_thread(lambda: attachment_path.stat().st_size)
        if file_size <= _INLINE_ATTACHMENT_MAX_BYTES:
            try:
                async with aiofile.AIOFile(attachment_path, "rb") as attachment_file:
                    file_bytes = bytes(await attachment_file.read())  # pyright: ignore[reportArgumentType]
            except Exception:
                logger.exception(
                    f"Failed to read inbound attachment for Gemini session={self._session_id} path={normalized_attachment_path} source={attachment.source}"
                )
                return None

            seen_attachment_paths.add(normalized_attachment_path)
            logger.info(
                f"Inlined inbound attachment for Gemini session={self._session_id} path={normalized_attachment_path} mime_type={mime_type} bytes={file_size} source={attachment.source}"
            )
            return types.Part.from_bytes(data=file_bytes, mime_type=mime_type)

        upload_config: dict[str, str] = {"mime_type": mime_type}
        if attachment.display_name and attachment.display_name.strip():
            upload_config["display_name"] = attachment.display_name.strip()

        try:
            uploaded_file = await self._service.ai_client.aio.files.upload(file=attachment_path, config=upload_config)  # pyright: ignore[reportArgumentType]
        except Exception:
            logger.exception(
                f"Failed to upload inbound attachment for Gemini session={self._session_id} path={normalized_attachment_path} source={attachment.source}"
            )
            return None

        uploaded_file_uri = getattr(uploaded_file, "uri", None)
        if not isinstance(uploaded_file_uri, str) or not uploaded_file_uri:
            logger.warning(
                f"Uploaded inbound attachment did not return a usable URI for session={self._session_id} path={normalized_attachment_path}"
            )
            return None

        seen_attachment_paths.add(normalized_attachment_path)
        logger.info(
            f"Uploaded inbound attachment for Gemini session={self._session_id} path={normalized_attachment_path} mime_type={mime_type} bytes={file_size} source={attachment.source}"
        )
        return types.Part.from_uri(file_uri=uploaded_file_uri, mime_type=mime_type)

    @staticmethod
    def _is_stale_uploaded_file_error(exc: genai_errors.ClientError) -> bool:
        if getattr(exc, "code", None) != 403:
            return False
        message = str(exc)
        if "PERMISSION_DENIED" not in message:
            return False
        return "File " in message or "files/" in message

    async def _send_chat_message(
        self,
        *,
        message: Any,
        config: types.GenerateContentConfig,
    ) -> Any:
        try:
            return await self._chat.send_message(message=message, config=config)
        except genai_errors.ClientError as exc:
            if not self._is_stale_uploaded_file_error(exc):
                raise

            history = self._get_curated_history()
            if not _has_file_data_parts(history):
                raise

            logger.warning(
                f"Detected stale Gemini uploaded-file reference for session={self._session_id}; "
                f"stripping file_data parts from history and retrying once"
            )
            sanitized_history = _strip_file_data_parts(history)
            self._chat = self._service.ai_client.aio.chats.create(
                model=settings.GEMINI_MODEL,
                history=cast("list[types.ContentOrDict] | None", sanitized_history),
            )
            return await self._chat.send_message(message=message, config=config)

    async def _persist_history(self) -> None:
        try:
            await self._service.save_session_history(
                self._session_id,
                self._get_curated_history(),
                self._usage_totals,
                system_instruction=self._latest_system_instruction,
            )
        except Exception:
            logger.exception(f"Failed to persist session history for {self._session_id}")

    async def _flush_outbound_injections(self) -> None:
        if not settings.SESSION_CONTINUITY or self._service._outbound_injection_handler is None:
            return

        targets = consume_pending_outbound_injections(self._session_id)
        if not targets:
            return

        injectable = _extract_injectable_content(self._get_curated_history())
        if injectable is None:
            return

        handler = self._service._outbound_injection_handler
        for target_session_key in targets:
            try:
                await handler(target_session_key, injectable)
            except Exception:
                logger.exception(
                    f"Failed to inject outbound turn into session={target_session_key} from source={self._session_id}"
                )

    def get_usage_totals(self) -> ChatSessionUsageTotals:
        return self._usage_totals.model_copy(deep=True)

    def total_token_count(self) -> int:
        return self._usage_totals.total_token_count

    async def replace_history_with_summary(self, summary_text: str) -> None:
        normalized_summary = summary_text.strip()
        if not normalized_summary:
            raise ValueError("summary_text must not be blank")

        compressed_history = [
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text=self._service.render_required_prompt_text(
                            _COMPRESSED_SESSION_HISTORY_PROMPT_NAME,
                            summary_text=normalized_summary,
                        )
                    )
                ],
            )
        ]
        self._chat = self._service.ai_client.aio.chats.create(
            model=settings.GEMINI_MODEL,
            history=cast("list[types.ContentOrDict] | None", compressed_history),
        )
        self._usage_totals = ChatSessionUsageTotals()
        await self._persist_history()

    async def inject_model_turn(self, content: types.Content) -> None:
        updated_history = [*self._get_curated_history(), content]
        self._chat = self._service.ai_client.aio.chats.create(
            model=settings.GEMINI_MODEL,
            history=cast("list[types.ContentOrDict] | None", updated_history),
        )
        await self._persist_history()

    def render_usage_report(self) -> str:
        return self._usage_totals.to_display_text()

    def _get_curated_history(self) -> list[types.Content]:
        history = self._chat.get_history(curated=True)
        return [types.Content.model_validate(content) for content in history]

    def _get_latest_model_response_text(
        self,
    ) -> str:
        history = self._chat.get_history(curated=True)
        model_contents: list[types.Content] = []

        for content in reversed(history):
            if getattr(content, "role", None) != "model":
                if model_contents:
                    break
                continue

            model_contents.append(content)

        if not model_contents:
            return ""

        model_contents.reverse()
        return "".join(self._extract_parts_text(content.parts) for content in model_contents)

    def _extract_parts_text(
        self,
        parts: list[types.Part] | None,
    ) -> str:
        if not parts:
            return ""

        texts: list[str] = []
        for part in parts:
            if getattr(part, "thought", False):
                continue

            part_text = getattr(part, "text", None)
            if isinstance(part_text, str):
                texts.append(part_text)

        return "".join(texts)


chat_service = GeminiChatService()
