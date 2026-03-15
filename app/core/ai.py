"""
AI client
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from inspect import cleandoc
from zoneinfo import ZoneInfo

import aiofile
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from google import genai
from google.genai import types

from app.core.config import settings
from app.schema.ai import ChatResponse

__all__ = (
    "GeminiChatService",
    "GeminiChatSession",
    "chat_service",
)


class GeminiChatService:
    def __init__(self) -> None:
        self.ai_client = genai.Client(api_key=settings.GEMINI_API_KEY)

    def create_session(self) -> "GeminiChatSession":
        return GeminiChatSession(self)

    async def get_base_context(self) -> str:
        now = datetime.now(ZoneInfo(settings.TIMEZONE))

        context = f"""
        ---
        datetime: {now:%Y-%b-%d %H:%M:%S}
        timezone: {settings.TIMEZONE}
        workspace: {settings.WORKSPACE_ROOT}
        ---
        """
        return cleandoc(context)

    @asynccontextmanager
    async def get_system_instruction(self) -> AsyncIterator[str | None]:
        async with aiofile.AIOFile(settings.WORKSPACE_ROOT / "AGENTS.md", "r", encoding="utf-8") as agents_file:
            content = str(await agents_file.read())
            content = await self.get_base_context() + "\n" + content
            yield content
            return

        yield None


class GeminiChatSession:
    def __init__(self, service: GeminiChatService) -> None:
        self._service = service
        self._mcp_client = Client(
            StreamableHttpTransport(
                f"http://{settings.MCP_HOST}:{settings.MCP_PORT}/mcp",
            )
        )
        self._chat = service.ai_client.aio.chats.create(model=settings.GEMINI_MODEL)

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
        if full_response.strip():
            return ChatResponse(
                text=full_response,
                usage_metadata=response.usage_metadata,
            )

        return ChatResponse(
            text=self._get_latest_model_response_text(),
            usage_metadata=response.usage_metadata,
        )

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
