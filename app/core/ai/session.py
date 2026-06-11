"""GeminiChatSession: per-session chat state, tool calling, and history management."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

import aiofile
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel

from app.core.ai.attachments import (
    _INLINE_ATTACHMENT_MAX_BYTES,
    _extract_inbound_attachments,
    _extract_injectable_content,
    _has_file_data_parts,
    _strip_file_data_parts,
    _supported_attachment_mime_type,
    resolve_inbound_attachment_path,
)
from app.core.config import settings
from app.core.log import logger
from app.runtime.session_binding import (
    bind_mcp_session_to_runtime_session,
    consume_pending_outbound_injections,
    get_runtime_session_todo_snapshot,
)
from app.schema.ai import ChatResponse, ChatSessionUsageTotals, InboundAttachment
from app.schema.todo import TodoListSnapshot
from app.util.rehydration import RehydrationBundle, render_rehydration_text, summarize_tool_observation

if TYPE_CHECKING:
    from app.core.ai.service import GeminiChatService


_COMPRESSED_SESSION_HISTORY_PROMPT_NAME = "compressed_session_history.prompt.md"
_EMPTY_MODEL_RESPONSE_FALLBACK_PROMPT_NAME = "empty_model_response_fallback.prompt.md"
_EMPTY_RESPONSE_NUDGE_PROMPT_NAME = "empty_response_nudge.prompt.md"
_UNKNOWN_TOOL_NUDGE_PROMPT_NAME = "unknown_tool_nudge.prompt.md"
# Max distinct hallucinated tool names to re-prompt past in a single send before giving up.
_UNKNOWN_TOOL_MAX_NUDGES = 2
_TODO_STATUS_LABELS = {
    "not-started": "not started",
    "in-progress": "in progress",
    "completed": "completed",
}


class _StreamedSendResult(BaseModel):
    """Aggregated streamed turn, shaped like the response fields send_message reads."""

    text: str
    parts: None = None
    usage_metadata: types.GenerateContentResponseUsageMetadata | None = None


def _is_streaming_unsupported_error(exc: Exception) -> bool:
    if getattr(exc, "code", None) == 501:
        return True
    return "not implemented" in str(exc).lower()


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
        self._mcp_client = self._build_mcp_client()
        self._mcp_client_opened = False
        self._chat = service.ai_client.aio.chats.create(
            model=settings.GEMINI_MODEL,
            history=cast("list[types.ContentOrDict] | None", history),
        )

    @staticmethod
    def _build_mcp_client() -> Client:
        return Client(
            StreamableHttpTransport(
                f"http://{settings.MCP_HOST}:{settings.MCP_PORT}/mcp",
            )
        )

    async def _ensure_mcp_client_open(self) -> Client:
        """P1 #7: open the MCP transport once per session, not once per turn.

        Rebuilds the client once on `ConnectionError` so a server restart between turns
        doesn't permanently break the session.
        """
        if self._mcp_client_opened:
            return self._mcp_client
        try:
            await self._mcp_client.__aenter__()
        except ConnectionError:
            logger.warning(f"MCP client connect failed for session={self._session_id}; rebuilding once")
            self._mcp_client = self._build_mcp_client()
            await self._mcp_client.__aenter__()
        self._mcp_client_opened = True
        mcp_session_id = self._mcp_client.transport.get_session_id()
        if mcp_session_id is not None:
            bind_mcp_session_to_runtime_session(mcp_session_id, self._session_id)
        return self._mcp_client

    async def aclose(self) -> None:
        if not self._mcp_client_opened:
            return
        try:
            await self._mcp_client.__aexit__(None, None, None)
        except Exception:
            logger.exception(f"Failed to close MCP client for session={self._session_id}")
        finally:
            self._mcp_client_opened = False

    async def send_message(
        self,
        message: str,
        message_metadata: list[dict[str, Any]] | None = None,
        channel_name: str | None = None,
        max_remote_calls: int | None = None,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> ChatResponse:
        # P2 #12: scheduled tasks can lower the per-run AFC cap via `max_remote_calls`.
        effective_max_remote_calls = max_remote_calls if max_remote_calls is not None else settings.GEMINI_MAX_AFC_CALLS
        async with asyncio.timeout(settings.GEMINI_RESPONSE_TIMEOUT_SECONDS):
            message_parts = await self._build_message_parts(message, message_metadata)
            system_instruction = await self._service.build_system_instruction(
                channel_name=channel_name,
                message_metadata=message_metadata,
            )
            self._latest_system_instruction = system_instruction

            mcp_client = await self._ensure_mcp_client_open()
            response = await self._send_chat_message(
                message=message_parts,
                on_text_delta=on_text_delta,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=settings.GEMINI_TEMPERATURE,
                    top_p=settings.GEMINI_TOP_P,
                    max_output_tokens=settings.GEMINI_MAX_OUTPUT_TOKENS,
                    thinking_config=types.ThinkingConfig(
                        thinking_level=types.ThinkingLevel(settings.GEMINI_THINKING_LEVEL)
                    ),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        maximum_remote_calls=effective_max_remote_calls,
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
                    channel_name=channel_name,
                    message_metadata=message_metadata,
                )
                self._latest_system_instruction = system_instruction

                mcp_client = await self._ensure_mcp_client_open()
                nudge_response = await self._send_chat_message(
                    message=self._service.render_required_prompt_text(_EMPTY_RESPONSE_NUDGE_PROMPT_NAME),
                    on_text_delta=on_text_delta,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=settings.GEMINI_TEMPERATURE,
                        top_p=settings.GEMINI_TOP_P,
                        max_output_tokens=settings.GEMINI_MAX_OUTPUT_TOKENS,
                        thinking_config=types.ThinkingConfig(
                            thinking_level=types.ThinkingLevel(settings.GEMINI_THINKING_LEVEL)
                        ),
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(
                            maximum_remote_calls=effective_max_remote_calls,
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

    def _build_todo_snapshot_part(self) -> types.Part | None:
        # P1 #5: the per-session todo list moved out of the system instruction so it does
        # not invalidate the cached prefix on every turn. Prepend it as the first user-turn
        # part instead so the model still sees current plan state.
        todo_snapshot = get_runtime_session_todo_snapshot(self._session_id)
        rendered = _render_todo_list_instruction(todo_snapshot)
        if not rendered:
            return None
        header = "Current plan state (do not restate; use to inform the next action):"
        return types.Part.from_text(text=f"{header}\n{rendered}")

    async def _build_message_parts(
        self,
        message: str,
        message_metadata: list[dict[str, Any]] | None,
    ) -> list[types.Part]:
        message_parts: list[types.Part] = []
        if (todo_part := self._build_todo_snapshot_part()) is not None:
            message_parts.append(todo_part)
        message_parts.append(types.Part.from_text(text=message))
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

        attachment_path = resolve_inbound_attachment_path(normalized_attachment_path, attachment.source)
        if attachment_path is None:
            logger.warning(
                f"Skipping inbound attachment outside the per-channel root for Gemini "
                f"session={self._session_id} path={normalized_attachment_path} source={attachment.source}"
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
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> Any:
        if on_text_delta is not None and not self._service.streaming_disabled:
            streamed_result = await self._send_chat_message_streaming(
                message=message,
                config=config,
                on_text_delta=on_text_delta,
            )
            if streamed_result is not None:
                return streamed_result

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
        except KeyError as exc:
            # google-genai AFC crashes with a bare KeyError when the model emits a functionCall
            # whose name isn't in the declared tool set (a hallucinated tool — common with the
            # local models behind pillbug-genai-proxy). The failed turn is not recorded in chat
            # history, so we re-send the same message with a note naming the missing tool and the
            # tools that actually exist, letting the model answer or pick a real tool instead.
            return await self._recover_from_unknown_tool_call(exc, message=message, config=config)

    async def _send_chat_message_streaming(
        self,
        *,
        message: Any,
        config: types.GenerateContentConfig,
        on_text_delta: Callable[[str], Awaitable[None]],
    ) -> _StreamedSendResult | None:
        """Streamed send; returns None when the turn should fall back to a non-streaming send.

        The SDK records chat history only once the stream generator is fully consumed, and
        AFC tool rounds run as the stream is iterated. A failure before the first emitted
        delta is safe to retry non-streaming (nothing reached the user); a 501 from an
        upstream that doesn't implement streamGenerateContent additionally disables
        streaming for the rest of the runtime. A failure after text reached the channel
        re-raises so the caller's error handling takes over.
        """
        emitted_chunks: list[str] = []
        usage_metadata: types.GenerateContentResponseUsageMetadata | None = None
        try:
            stream = await self._chat.send_message_stream(message=message, config=config)
            async for chunk in stream:
                chunk_usage = getattr(chunk, "usage_metadata", None)
                if chunk_usage is not None:
                    usage_metadata = chunk_usage
                delta = self._extract_chunk_text(chunk)
                if delta:
                    emitted_chunks.append(delta)
                    await on_text_delta(delta)
        except Exception as exc:
            if emitted_chunks:
                raise
            if _is_streaming_unsupported_error(exc):
                self._service.disable_streaming(str(exc))
            else:
                logger.warning(
                    f"Streaming send failed before any output for session={self._session_id}; "
                    f"falling back to a non-streaming send: {exc}"
                )
            return None

        return _StreamedSendResult(
            text="".join(emitted_chunks),
            usage_metadata=usage_metadata,
        )

    def _extract_chunk_text(self, chunk: Any) -> str:
        candidates = getattr(chunk, "candidates", None)
        if not candidates:
            return ""
        content = getattr(candidates[0], "content", None)
        if content is None:
            return ""
        return self._extract_parts_text(getattr(content, "parts", None))

    @staticmethod
    def _unknown_tool_name(error: KeyError) -> str | None:
        if error.args and isinstance(error.args[0], str):
            return error.args[0]
        return None

    @staticmethod
    def _normalize_message_to_parts(message: Any) -> list[types.Part]:
        if isinstance(message, list):
            return list(message)
        if isinstance(message, str):
            return [types.Part.from_text(text=message)]
        return [message]

    async def _list_available_tool_names(self) -> list[str]:
        try:
            mcp_client = await self._ensure_mcp_client_open()
            tools = await mcp_client.list_tools()
        except Exception:
            logger.exception(f"Failed to list MCP tools for session={self._session_id}")
            return []
        return [tool.name for tool in tools if getattr(tool, "name", None)]

    def _build_unknown_tool_message(
        self,
        message: Any,
        tool_name: str,
        available_tools: list[str],
    ) -> list[types.Part]:
        note = self._service.render_required_prompt_text(
            _UNKNOWN_TOOL_NUDGE_PROMPT_NAME,
            tool_name=tool_name,
            available_tools=", ".join(sorted(available_tools)),
        )
        return [*self._normalize_message_to_parts(message), types.Part.from_text(text=note)]

    async def _recover_from_unknown_tool_call(
        self,
        error: KeyError,
        *,
        message: Any,
        config: types.GenerateContentConfig,
    ) -> Any:
        tool_name = self._unknown_tool_name(error)
        if tool_name is None:
            raise error

        attempted: set[str] = set()
        current_message: Any = message
        last_error: KeyError = error
        while tool_name is not None and tool_name not in attempted and len(attempted) < _UNKNOWN_TOOL_MAX_NUDGES:
            attempted.add(tool_name)
            available_tools = await self._list_available_tool_names()
            logger.warning(
                f"Model called unavailable tool {tool_name!r} for session={self._session_id}; "
                f"re-prompting with {len(available_tools)} available tool(s)"
            )
            current_message = self._build_unknown_tool_message(current_message, tool_name, available_tools)
            try:
                return await self._chat.send_message(message=current_message, config=config)
            except KeyError as retry_error:
                last_error = retry_error
                tool_name = self._unknown_tool_name(retry_error)

        raise last_error

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

    def snapshot_for_compaction(self) -> tuple[list[types.Content], ChatSessionUsageTotals]:
        # P1 #10: deep-copy history + usage totals before compress so a failed turn can roll back.
        history = [content.model_copy(deep=True) for content in self._get_curated_history()]
        usage_totals = self._usage_totals.model_copy(deep=True)
        return history, usage_totals

    async def restore_from_snapshot(
        self,
        snapshot: tuple[list[types.Content], ChatSessionUsageTotals],
    ) -> None:
        history, usage_totals = snapshot
        self._chat = self._service.ai_client.aio.chats.create(
            model=settings.GEMINI_MODEL,
            history=cast("list[types.ContentOrDict] | None", history),
        )
        self._usage_totals = usage_totals.model_copy(deep=True)
        await self._persist_history()

    async def replace_history_with_summary(
        self,
        summary_text: str,
        *,
        rehydration: RehydrationBundle | None = None,
    ) -> None:
        normalized_summary = summary_text.strip()
        if not normalized_summary:
            raise ValueError("summary_text must not be blank")

        compressed_history: list[types.Content] = [
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

        # P1 #9: append a rehydration turn so the model retains live plan state,
        # loaded skills, pending approvals, and recent tool observations across compaction.
        if rehydration is not None:
            rehydration_text = render_rehydration_text(rehydration)
            if rehydration_text:
                compressed_history.append(
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=rehydration_text)],
                    )
                )

        self._chat = self._service.ai_client.aio.chats.create(
            model=settings.GEMINI_MODEL,
            history=cast("list[types.ContentOrDict] | None", compressed_history),
        )
        self._usage_totals = ChatSessionUsageTotals()
        await self._persist_history()

    def collect_recent_tool_observations(self, *, max_count: int = 5, max_chars: int = 500) -> tuple[str, ...]:
        """Walk the chat history for the most recent function_response parts.

        Returns a tuple sized at most `max_count`, oldest-first, with each entry capped
        at `max_chars` characters. Safe to call before `replace_history_with_summary`.
        """
        history = self._get_curated_history()
        observations: list[str] = []
        for content in reversed(history):
            for part in getattr(content, "parts", None) or ():
                function_response = getattr(part, "function_response", None)
                if function_response is None:
                    continue
                tool_name = getattr(function_response, "name", None) or "tool"
                response_payload = getattr(function_response, "response", None)
                observations.append(f"{tool_name}: {summarize_tool_observation(response_payload, max_chars=max_chars)}")
                if len(observations) >= max_count:
                    break
            if len(observations) >= max_count:
                break
        return tuple(reversed(observations))

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

    def get_curated_history_snapshot(self) -> list[types.Content]:
        """Return a deep-copied curated history list safe for read-only consumers."""
        return [content.model_copy(deep=True) for content in self._get_curated_history()]

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
