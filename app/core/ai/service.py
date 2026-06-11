"""GeminiChatService: client construction, session lifecycle, prompt/context assembly."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiofile
from google import genai
from google.genai import types
from google.oauth2 import service_account

from app.core.ai.session import GeminiChatSession
from app.core.config import settings
from app.core.jinja import render_template
from app.core.log import logger
from app.runtime.channels import get_available_channels_context, get_channel_plugin
from app.runtime.session_binding import (
    bind_runtime_session_todo_snapshot,
)
from app.schema.ai import ChatSessionSnapshot, ChatSessionUsageTotals, Skill
from app.schema.messages import extract_a2a_origin_route
from app.util.base_dir import get_module_root
from app.util.skills import discover_workspace_skills

_DIRECT_REPLY_CHANNEL_MEMO_PROMPT_NAME = "direct_reply_channel_memo.prompt.md"
_MODEL_INPUT_PROMPT_NAME = "model_input.prompt.md"
_SKILLS_PROMPT_NAME = "skills.prompt.md"
_CHANNEL_MEMO_PROMPTS = {"a2a": "a2a_channel_memo.prompt.md", "telegram": "telegram_channel_memo.prompt.md"}
_DIRECT_REPLY_CHANNEL_EXCLUSIONS = frozenset({"a2a", "trigger"})


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


@cache
def _load_agents_md_cached(path: str, mtime_ns: int) -> str:
    # P1 #8: mtime-keyed cache mirrors `_load_security_patterns_from_disk` in
    # app/runtime/pipeline.py. Keeps AGENTS.md off the hot path while still picking up
    # edits that change the file's mtime.
    del mtime_ns
    return Path(path).read_text(encoding="utf-8")


async def _read_agents_md(agents_md_path: Path) -> str:
    def _read() -> str:
        try:
            mtime_ns = agents_md_path.stat().st_mtime_ns
        except OSError:
            return ""
        return _load_agents_md_cached(str(agents_md_path), mtime_ns)

    return await asyncio.to_thread(_read)


class GeminiChatService:
    def __init__(self) -> None:
        self.ai_client = self._build_genai_client()
        self._sessions_dir = settings.SESSIONS_DIR
        self._module_root = get_module_root("app")
        self._prompts_dir = self._module_root / "prompts"
        self._outbound_injection_handler: Callable[[str, types.Content], Awaitable[None]] | None = None
        self._streaming_disabled_reason: str | None = None

    @staticmethod
    def _build_genai_client() -> genai.Client:
        if settings.GEMINI_BACKEND == "vertex":
            credentials = None
            if settings.GEMINI_VERTEX_CREDENTIALS_PATH is not None:
                credentials = service_account.Credentials.from_service_account_file(
                    str(settings.GEMINI_VERTEX_CREDENTIALS_PATH),
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )

            return genai.Client(
                vertexai=True,
                project=settings.GEMINI_VERTEX_PROJECT,
                location=settings.GEMINI_VERTEX_LOCATION,
                credentials=credentials,
            )

        http_options = types.HttpOptions(base_url=settings.GEMINI_BASE_URL) if settings.GEMINI_BASE_URL else None
        return genai.Client(api_key=settings.GEMINI_API_KEY, http_options=http_options)

    def set_outbound_injection_handler(self, handler: Callable[[str, types.Content], Awaitable[None]] | None) -> None:
        self._outbound_injection_handler = handler

    @property
    def streaming_disabled(self) -> bool:
        return self._streaming_disabled_reason is not None

    def disable_streaming(self, reason: str) -> None:
        """Sticky opt-out: once the upstream rejects streamGenerateContent (the pillbug
        proxies return 501), every later turn skips the streaming attempt."""
        if self._streaming_disabled_reason is not None:
            return
        self._streaming_disabled_reason = reason
        logger.warning(
            f"Upstream rejected streamGenerateContent; using non-streaming sends for the rest of this runtime: {reason}"
        )

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

    async def load_history_snapshot(self, session_id: str) -> list[types.Content]:
        snapshot = await self._load_session_snapshot(session_id)
        if snapshot is None or not snapshot.history:
            return []
        return list(snapshot.history)

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

        for channel_name in sorted(settings.enabled_channels()):
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
        channel_name: str | None = None,
        message_metadata: list[dict[str, Any]] | None = None,
    ) -> str | None:
        # P1 #5: the system instruction holds only stable content (agents_md → skills →
        # channel_memos → base_context). The per-session todo snapshot moved to the user
        # turn so it doesn't fragment Gemini's automatic prefix cache. base_context still
        # carries the volatile datetime line; it lives at the end so the cached prefix is
        # everything before it.
        agents_md = await _read_agents_md(settings.WORKSPACE_ROOT / "AGENTS.md")
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
            skills=skills_prompt.strip() if skills_prompt else None,
        )


chat_service = GeminiChatService()
