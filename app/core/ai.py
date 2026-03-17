"""
AI client
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import cast
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiofile
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from google import genai
from google.genai import types

from app.core.config import settings
from app.core.log import logger
from app.runtime.channels import get_available_channels_context
from app.schema.ai import ChatResponse, ChatSessionSnapshot, Skill

__all__ = (
    "GeminiChatService",
    "GeminiChatSession",
    "chat_service",
)


class GeminiChatService:
    def __init__(self) -> None:
        self.ai_client = genai.Client(api_key=settings.GEMINI_API_KEY)
        self._sessions_dir = settings.SESSIONS_DIR

    def create_session(
        self,
        session_id: str,
        history: list[types.Content] | None = None,
    ) -> GeminiChatSession:
        return GeminiChatSession(self, session_id=session_id, history=history)

    async def restore_session(self, session_id: str) -> GeminiChatSession:
        history = await self._load_session_history(session_id)
        if history:
            logger.info(f"Restored session history for {session_id} with {len(history)} messages")

        return self.create_session(session_id=session_id, history=history or None)

    async def reset_session(self, session_id: str) -> GeminiChatSession:
        await self._delete_session_history(session_id)
        return self.create_session(session_id=session_id)

    async def save_session_history(
        self,
        session_id: str,
        history: list[types.Content],
    ) -> None:
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        snapshot = ChatSessionSnapshot(session_id=session_id, history=history)
        session_path = self._get_session_path(session_id)

        async with aiofile.AIOFile(session_path, "w", encoding="utf-8") as session_file:
            await session_file.write(snapshot.model_dump_json(indent=2))

    async def _load_session_history(self, session_id: str) -> list[types.Content] | None:
        session_path = self._get_session_path(session_id)
        if not session_path.is_file():
            return None

        try:
            async with aiofile.AIOFile(session_path, "r", encoding="utf-8") as session_file:
                snapshot = ChatSessionSnapshot.model_validate_json(str(await session_file.read()))
        except Exception:
            logger.exception(f"Failed to restore session history from {session_path}")
            return None

        return snapshot.history

    async def _delete_session_history(self, session_id: str) -> None:
        session_path = self._get_session_path(session_id)
        if session_path.exists():
            session_path.unlink()

    def _get_session_path(self, session_id: str):
        return self._sessions_dir / quote(session_id, safe="")

    async def get_base_context(self) -> str:
        now = datetime.now(ZoneInfo(settings.TIMEZONE))

        return "\n".join((
            "---",
            f"datetime: {now:%Y-%b-%d %H:%M:%S}",
            f"timezone: {settings.TIMEZONE}",
            f"workspace: {settings.WORKSPACE_ROOT}",
            f"available_channels: {', '.join(get_available_channels_context())}",
            "---",
        ))

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
            content = str(await agents_file.read())
            content = await self.get_base_context() + "\n" + content
            if skills := await self.discover_skills():
                content += (
                    "\n\n---\n\n"
                    "## Available Skills\n\nThe following skills extend your capabilities. "
                    "To use a skill, read its SKILL.md file using the `read_file` tool."
                    "\n"
                )
                for skill in skills:
                    content += (
                        f"### {skill.name}\n\n- Description: {skill.description}\n- Location: {skill.location}\n\n"
                    )
            yield content
            return

        yield None


class GeminiChatSession:
    def __init__(
        self,
        service: GeminiChatService,
        session_id: str,
        history: list[types.Content] | None = None,
    ) -> None:
        self._service = service
        self._session_id = session_id
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
    ) -> ChatResponse:
        async with self._mcp_client as mcp_client, self._service.get_system_instruction() as system_instruction:
            response = await self._chat.send_message(
                message=message,
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
        await self._persist_history()
        if full_response.strip():
            return ChatResponse(
                text=full_response,
                usage_metadata=response.usage_metadata,
            )

        return ChatResponse(
            text=self._get_latest_model_response_text(),
            usage_metadata=response.usage_metadata,
        )

    async def _persist_history(self) -> None:
        try:
            await self._service.save_session_history(self._session_id, self._get_curated_history())
        except Exception:
            logger.exception(f"Failed to persist session history for {self._session_id}")

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
