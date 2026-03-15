import re
import unicodedata

from app.core.config import settings
from app.core.log import logger
from app.schema.messages import (
    InboundBatch,
    MessageProcessingContext,
    MessageProcessingState,
    ProcessedInboundMessage,
    SecurityCheckResult,
)
from app.util.text import full_cleanup_text, slight_cleanup_text

# TODO: Load patterns from external file
_SECURITY_WARNING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("prompt-injection-language", re.compile(r"(?i)ignore\s+(all\s+)?previous\s+instructions")),
    ("system-prompt-reference", re.compile(r"(?i)\bsystem\s+prompt\b")),
    ("script-tag", re.compile(r"(?i)<script\b")),
)


def _has_disallowed_control_characters(text: str) -> bool:
    allowed_controls = {"\n", "\r", "\t"}
    return any(unicodedata.category(char) == "Cc" and char not in allowed_controls for char in text)


class CleanupStep:
    async def process(
        self,
        state: MessageProcessingState,
    ) -> MessageProcessingState:
        state.cleaned_text = slight_cleanup_text(state.batch.raw_text)
        state.normalized_text = full_cleanup_text(state.batch.raw_text)
        return state


class SecurityCheckStep:
    async def process(
        self,
        state: MessageProcessingState,
    ) -> MessageProcessingState:
        reasons: list[str] = []
        warnings: list[str] = []

        if not state.cleaned_text:
            reasons.append("message is empty after cleanup")

        if len(state.cleaned_text) > settings.INBOUND_MAX_MESSAGE_CHARS:
            reasons.append(
                f"message exceeds max length of {settings.INBOUND_MAX_MESSAGE_CHARS} characters after cleanup"
            )

        if _has_disallowed_control_characters(state.batch.raw_text):
            reasons.append("message contains disallowed control characters")

        for warning_name, pattern in _SECURITY_WARNING_PATTERNS:
            if pattern.search(state.batch.raw_text):
                warnings.append(warning_name)

        state.security = SecurityCheckResult(
            blocked=bool(reasons),
            reasons=tuple(reasons),
            warnings=tuple(warnings),
        )
        return state


class ContextEnrichmentStep:
    async def process(
        self,
        state: MessageProcessingState,
    ) -> MessageProcessingState:
        batch = state.batch
        context = MessageProcessingContext(
            channel=batch.channel_name,
            conversation_id=batch.conversation_id,
            user_id=batch.user_id,
            received_at=batch.received_at,
            debounced_message_count=batch.message_count,
            security_warnings=list(state.security.warnings),
            metadata=batch.last_message.metadata,
            normalized_text=state.normalized_text,
            model_input=state.cleaned_text,  # NOTE: In a real implementation, you would likely want to construct a more complex model input that includes context, instructions, etc. This is simplified for demonstration purposes.
        )

        rendered_context = context.model_dump_json(indent=2, exclude_none=True, exclude={"model_input"})
        state.context = context.model_copy(
            update={
                "model_input": f"Inbound message context:\n{rendered_context}\n\nUser message:\n{state.cleaned_text}"
            }
        )
        return state


class InboundProcessingPipeline:
    def __init__(self) -> None:
        self._steps = (
            CleanupStep(),
            SecurityCheckStep(),
            # ContextEnrichmentStep(),  # NOTE: Disabled for now
        )

    async def process(
        self,
        batch: InboundBatch,
    ) -> ProcessedInboundMessage:
        state = MessageProcessingState(batch=batch)

        for step in self._steps:
            state = await step.process(state)

        if state.context is None:
            logger.warning("Message processing pipeline did not populate context")

        return ProcessedInboundMessage(
            batch=batch,
            cleaned_text=state.cleaned_text,
            normalized_text=state.normalized_text,
            model_input=state.context.model_input if state.context is not None else state.cleaned_text,
            security=state.security,
            context=state.context,
        )
