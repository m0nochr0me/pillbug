"""
Schema definitions for messages received from channels.
"""

from datetime import UTC, datetime
from typing import Any, Self
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


def _utcnow() -> datetime:
    return datetime.now(UTC)


class InboundMessage(BaseModel):
    channel_name: str
    conversation_id: str
    text: str
    user_id: str | None = None
    message_id: str = Field(default_factory=lambda: uuid4().hex)
    received_at: datetime = Field(default_factory=_utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def session_key(self) -> str:
        return f"{self.channel_name}:{self.conversation_id}"

    @property
    def debounce_key(self) -> str:
        user_key = self.user_id or "anonymous"
        return f"{self.session_key}:{user_key}"


class InboundBatch(BaseModel):
    messages: tuple[InboundMessage, ...]

    @model_validator(mode="after")
    def validate_messages(self) -> Self:
        if not self.messages:
            raise ValueError("InboundBatch requires at least one message")

        first_message = self.messages[0]
        for message in self.messages[1:]:
            if message.channel_name != first_message.channel_name:
                raise ValueError("All messages in a batch must share the same channel")
            if message.conversation_id != first_message.conversation_id:
                raise ValueError("All messages in a batch must share the same conversation")
            if message.user_id != first_message.user_id:
                raise ValueError("All messages in a batch must share the same user")

        return self

    @property
    def first_message(self) -> InboundMessage:
        return self.messages[0]

    @property
    def last_message(self) -> InboundMessage:
        return self.messages[-1]

    @property
    def channel_name(self) -> str:
        return self.first_message.channel_name

    @property
    def conversation_id(self) -> str:
        return self.first_message.conversation_id

    @property
    def user_id(self) -> str | None:
        return self.first_message.user_id

    @property
    def session_key(self) -> str:
        return self.first_message.session_key

    @property
    def raw_text(self) -> str:
        return "\n\n".join(message.text for message in self.messages)

    @property
    def received_at(self) -> datetime:
        return self.last_message.received_at

    @property
    def message_count(self) -> int:
        return len(self.messages)


class SecurityCheckResult(BaseModel):
    blocked: bool = False
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class MessageProcessingContext(BaseModel):
    channel: str = Field(
        ...,
        description="The name of the channel the message was received from",
    )
    conversation_id: str = Field(
        ...,
        description="The ID of the conversation this message belongs to",
    )
    user_id: str | None = Field(
        None,
        description="The ID of the user who sent the message, if available",
    )
    received_at: datetime = Field(
        ...,
        description="The timestamp when the message was received",
    )
    debounced_message_count: int = Field(
        0,
        description="The number of messages that were debounced together in this batch",
    )
    security_warnings: list[str] = Field(
        default_factory=list,
        description="Any security-related warnings that were identified for this message",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata associated with the message",
    )
    normalized_text: str = Field(
        ...,
        description="The cleaned and normalized text of the message, ready to be used as input for the model",
    )
    model_input: str = Field(
        ...,
        description="The final input that will be sent to the model, which may include additional context or formatting beyond the normalized text",
    )


class ProcessedInboundMessage(BaseModel):
    batch: InboundBatch
    cleaned_text: str = Field(
        ...,
        description="The cleaned version of the input text, after basic cleanup steps",
    )
    normalized_text: str = Field(
        ...,
        description="The normalized version of the input text, after full cleanup steps",
    )
    model_input: str = Field(
        ...,
        description="The final input that will be sent to the model, which may include additional context or formatting beyond the normalized text",
    )
    security: SecurityCheckResult = Field(
        default_factory=SecurityCheckResult,
        description="The result of the security checks performed on the message",
    )
    context: MessageProcessingContext = Field(
        ...,
        description="The context information generated during message processing",
    )


class MessageProcessingState(BaseModel):
    batch: InboundBatch = Field(
        ...,
        description="The original batch of inbound messages being processed",
    )
    cleaned_text: str = Field(
        default="",
        description="The cleaned version of the input text, after basic cleanup steps",
    )
    normalized_text: str = Field(
        default="",
        description="The normalized version of the input text, after full cleanup steps",
    )
    security: SecurityCheckResult = Field(
        default_factory=SecurityCheckResult,
        description="The result of the security checks performed on the message",
    )
    context: MessageProcessingContext | None = Field(
        default=None,
        description="The context information generated during message processing",
    )
