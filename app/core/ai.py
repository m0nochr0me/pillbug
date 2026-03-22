"""
AI client
"""

import asyncio
import mimetypes
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiofile
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from google import genai
from google.genai import types
from pydantic import ValidationError

from app.core.config import settings
from app.core.jinja import render_template
from app.core.log import logger
from app.runtime.channels import get_available_channels_context, get_channel_plugin
from app.schema.ai import ChatResponse, ChatSessionSnapshot, ChatSessionUsageTotals, InboundAttachment, Skill
from app.util.base_dir import get_module_root
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
_EMPTY_MODEL_RESPONSE_FALLBACK = "I could not produce a text response right now. Please try again."
_MODEL_INPUT_PROMPT_NAME = "model_input.prompt.md"
_SKILLS_PROMPT_NAME = "skills.prompt.md"
_CHANNEL_MEMO_PROMPTS = {"a2a": "a2a_channel_memo.prompt.md"}


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


class GeminiChatService:
    def __init__(self) -> None:
        self.ai_client = genai.Client(api_key=settings.GEMINI_API_KEY)
        self._sessions_dir = settings.SESSIONS_DIR
        self._module_root = get_module_root("app")
        self._prompts_dir = self._module_root / "prompts"

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
        return self.create_session(session_id=session_id)

    async def save_session_history(
        self,
        session_id: str,
        history: list[types.Content],
        usage_totals: ChatSessionUsageTotals,
    ) -> None:
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        snapshot = ChatSessionSnapshot(session_id=session_id, history=history, usage_totals=usage_totals)
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

    async def get_base_context(self) -> str:
        now = datetime.now(ZoneInfo(settings.TIMEZONE))

        return "\n".join(
            (
                "---",
                f"datetime: {now:%Y-%b-%d %H:%M:%S}",
                f"timezone: {settings.TIMEZONE}",
                f"workspace: {settings.WORKSPACE_ROOT}",
                f"available_channels: {', '.join(await get_available_channels_context())}",
                "---\n",
            ),
        )

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
        if not (skills_base_dir := settings.WORKSPACE_ROOT / "skills").is_dir():
            return []
        skill_dirs = [d for d in skills_base_dir.iterdir() if d.is_dir()]
        skills = []
        for skill_dir in skill_dirs:
            if not (skill_file := skill_dir / "SKILL.md").is_file():
                logger.warning(f"Skipping skill directory {skill_dir} because it does not contain a SKILL.md file")
                continue

            async with aiofile.AIOFile(skill_file, "r", encoding="utf-8") as skill_md_file:
                name = None
                description = None
                async for line in aiofile.LineReader(skill_md_file):
                    if not line:
                        raise StopAsyncIteration
                    line = cast("str", line).strip()  # noqa: PLW2901
                    if line.startswith("name:"):
                        name = line[len("name:") :].strip()
                    elif line.startswith("description:"):
                        description = line[len("description:") :].strip()
                    if name and description:
                        skills.append(Skill(name=name, description=description, location=skill_dir))
                        break
        return skills

    @asynccontextmanager
    async def get_system_instruction(self) -> AsyncIterator[str | None]:
        async with aiofile.AIOFile(settings.WORKSPACE_ROOT / "AGENTS.md", "r", encoding="utf-8") as agents_file:
            agents_md = str(await agents_file.read())
            skills_prompt: str | None = None
            if skills := await self.discover_skills():
                skills_prompt = self.render_prompt_text(
                    _SKILLS_PROMPT_NAME,
                    skills=skills,
                )
            channel_memos = await self.get_channel_instruction_memos()
            yield self.render_prompt_text(
                _MODEL_INPUT_PROMPT_NAME,
                base_context=await self.get_base_context(),
                agents_md=agents_md,
                channel_memos=tuple(channel_memos),
                skills=skills_prompt.strip() if skills_prompt else None,
            )
            return

        yield None


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
    ) -> ChatResponse:
        message_parts = await self._build_message_parts(message, message_metadata)

        async with self._mcp_client as mcp_client, self._service.get_system_instruction() as system_instruction:
            response = await self._chat.send_message(
                message=message_parts,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=settings.GEMINI_TEMPERATURE,
                    top_p=settings.GEMINI_TOP_P,
                    max_output_tokens=settings.GEMINI_MAX_OUTPUT_TOKENS,
                    thinking_config=types.ThinkingConfig(
                        thinking_level=types.ThinkingLevel(settings.GEMINI_THINKING_LEVEL)
                    ),
                    tools=[mcp_client.session],
                ),
            )

        full_response = response.text or self._extract_parts_text(response.parts)
        self._usage_totals.add_usage_metadata(response.usage_metadata)
        await self._persist_history()
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

        logger.warning(f"Gemini returned no text for session={self._session_id}; responding with fallback text instead")

        return ChatResponse(
            text=_EMPTY_MODEL_RESPONSE_FALLBACK,
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
            f"Uploaded inbound attachment for Gemini session={self._session_id} path={normalized_attachment_path} mime_type={mime_type} source={attachment.source}"
        )
        return types.Part.from_uri(file_uri=uploaded_file_uri, mime_type=mime_type)

    async def _persist_history(self) -> None:
        try:
            await self._service.save_session_history(
                self._session_id,
                self._get_curated_history(),
                self._usage_totals,
            )
        except Exception:
            logger.exception(f"Failed to persist session history for {self._session_id}")

    def get_usage_totals(self) -> ChatSessionUsageTotals:
        return self._usage_totals.model_copy(deep=True)

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
