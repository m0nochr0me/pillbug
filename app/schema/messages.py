"""
Schema definitions for messages received from channels.
"""

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

_RUNTIME_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
_A2A_CONVERGENCE_METADATA_KEY = "pillbug_convergence"
_A2A_ORIGIN_CHANNEL_NAME_METADATA_KEY = "pillbug_origin_channel_name"
_A2A_ORIGIN_CONVERSATION_ID_METADATA_KEY = "pillbug_origin_conversation_id"
_A2A_ORIGIN_CHANNEL_METADATA_KEY = "pillbug_origin_channel_metadata"
A2A_CONVERGENCE_EXTENSION_URI = "https://pillbug.dev/extensions/a2a-convergence/v1"


def build_a2a_origin_routing_metadata(
    *,
    channel_name: str,
    conversation_id: str,
    channel_metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    normalized_channel_name = channel_name.strip()
    normalized_conversation_id = conversation_id.strip()
    if not normalized_channel_name or not normalized_conversation_id:
        raise ValueError("A2A origin routing metadata requires non-empty channel_name and conversation_id")

    metadata: dict[str, object] = {
        _A2A_ORIGIN_CHANNEL_NAME_METADATA_KEY: normalized_channel_name,
        _A2A_ORIGIN_CONVERSATION_ID_METADATA_KEY: normalized_conversation_id,
    }
    if channel_metadata:
        metadata[_A2A_ORIGIN_CHANNEL_METADATA_KEY] = channel_metadata

    return metadata


def extract_a2a_origin_routing_metadata(metadata: dict[str, Any]) -> dict[str, str] | None:
    channel_name = metadata.get(_A2A_ORIGIN_CHANNEL_NAME_METADATA_KEY)
    conversation_id = metadata.get(_A2A_ORIGIN_CONVERSATION_ID_METADATA_KEY)
    if not isinstance(channel_name, str) or not isinstance(conversation_id, str):
        return None

    normalized_channel_name = channel_name.strip()
    normalized_conversation_id = conversation_id.strip()
    if not normalized_channel_name or not normalized_conversation_id:
        return None

    return {
        _A2A_ORIGIN_CHANNEL_NAME_METADATA_KEY: normalized_channel_name,
        _A2A_ORIGIN_CONVERSATION_ID_METADATA_KEY: normalized_conversation_id,
    }


def extract_a2a_origin_route(metadata: dict[str, Any]) -> tuple[str, str] | None:
    if origin_metadata := extract_a2a_origin_routing_metadata(metadata):
        return (
            origin_metadata[_A2A_ORIGIN_CHANNEL_NAME_METADATA_KEY],
            origin_metadata[_A2A_ORIGIN_CONVERSATION_ID_METADATA_KEY],
        )

    return None


def extract_a2a_origin_channel_metadata(metadata: dict[str, Any]) -> dict[str, object] | None:
    channel_metadata = metadata.get(_A2A_ORIGIN_CHANNEL_METADATA_KEY)
    if not isinstance(channel_metadata, dict):
        return None

    normalized_channel_metadata: dict[str, object] = {}
    for key, value in channel_metadata.items():
        if not isinstance(key, str) or not key.strip():
            continue
        normalized_channel_metadata[key] = value

    return normalized_channel_metadata or None


def _utcnow() -> datetime:
    return datetime.now(UTC)


class OutboundAttachment(BaseModel):
    path: str = Field(
        ...,
        description="Workspace-relative or absolute path to the file to send",
    )
    mime_type: str | None = Field(
        default=None,
        description="MIME type of the file; channels may use this to select the send method",
    )
    display_name: str | None = Field(
        default=None,
        description="Display name or caption for the attachment",
    )
    send_as: str | None = Field(
        default=None,
        description="Hint for the channel on how to send the file (e.g. 'voice', 'document', 'photo')",
    )

    @model_validator(mode="after")
    def normalize_values(self) -> Self:
        self.path = self.path.strip()
        if not self.path:
            raise ValueError("path must not be blank")

        if self.mime_type is not None:
            self.mime_type = self.mime_type.strip().lower() or None

        if self.display_name is not None:
            self.display_name = self.display_name.strip() or None

        if self.send_as is not None:
            self.send_as = self.send_as.strip().lower() or None

        return self


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


class A2AIntent(StrEnum):
    ASK = "ask"
    INFORM = "inform"
    DELEGATE = "delegate"
    RESULT = "result"
    ERROR = "error"
    HEARTBEAT = "heartbeat"


class A2AAttachment(BaseModel):
    name: str | None = None
    media_type: str | None = None
    url: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_values(self) -> Self:
        if self.name is not None:
            self.name = self.name.strip() or None

        if self.media_type is not None:
            self.media_type = self.media_type.strip().lower() or None

        if self.url is not None:
            self.url = self.url.strip() or None

        return self

    def render_summary(self) -> str:
        label = self.name or self.url or "attachment"
        if self.media_type:
            return f"{label} ({self.media_type})"
        return label


class A2ATarget(BaseModel):
    runtime_id: str = Field(min_length=3, max_length=64)
    conversation_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        self.runtime_id = self.runtime_id.strip()
        self.conversation_id = self.conversation_id.strip()

        if not _RUNTIME_ID_PATTERN.fullmatch(self.runtime_id):
            raise ValueError(
                "runtime_id must start with an alphanumeric character and only contain letters, numbers, '.', '_' or '-'"
            )

        if not self.conversation_id:
            raise ValueError("conversation_id must not be blank")

        return self

    @classmethod
    def parse(cls, value: str) -> Self:
        normalized_value = value.strip()
        if normalized_value.startswith("a2a:"):
            normalized_value = normalized_value[4:].strip()

        runtime_id, separator, conversation_id = normalized_value.partition("/")
        if not separator:
            raise ValueError(
                "A2A destination must use the format runtime_id/conversation_id or a2a:runtime_id/conversation_id"
            )

        return cls(runtime_id=runtime_id, conversation_id=conversation_id)

    def as_conversation_target(self) -> str:
        return f"{self.runtime_id}/{self.conversation_id}"


class A2AConvergenceState(BaseModel):
    max_hops: int = Field(default=2, ge=1, le=32)
    hop_count: int = Field(default=0, ge=0, le=32)
    stop_requested: bool = False
    stop_reason: str | None = None

    @model_validator(mode="after")
    def normalize_values(self) -> Self:
        if self.stop_reason is not None:
            self.stop_reason = self.stop_reason.strip() or None

        if self.hop_count > self.max_hops:
            raise ValueError("hop_count must not exceed max_hops")

        return self

    @property
    def remaining_hops(self) -> int:
        return max(self.max_hops - self.hop_count, 0)

    def reply_block_reason(self, intent: A2AIntent) -> str | None:
        if self.stop_requested:
            return "stop_requested"

        if intent in {A2AIntent.RESULT, A2AIntent.INFORM, A2AIntent.ERROR, A2AIntent.HEARTBEAT}:
            return "terminal_intent"

        if self.hop_count >= self.max_hops:
            return "convergence_limit"

        return None

    def next_outbound(self, *, stop_requested: bool = False, stop_reason: str | None = None) -> Self:
        if self.hop_count >= self.max_hops:
            raise ValueError("A2A convergence limit reached; cannot emit another automatic outbound message")

        return self.model_copy(
            update={
                "hop_count": self.hop_count + 1,
                "stop_requested": self.stop_requested or stop_requested,
                "stop_reason": (stop_reason.strip() or None) if stop_reason is not None else self.stop_reason,
            }
        )

    def to_metadata(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def render_limit_message(self) -> str:
        return (
            "Convergence limit reached for this A2A exchange. "
            f"Current hop is {self.hop_count} of {self.max_hops}. No further automatic replies will be sent."
        )

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any]) -> Self:
        raw_state = metadata.get(_A2A_CONVERGENCE_METADATA_KEY)
        if not isinstance(raw_state, dict):
            return cls()

        return cls.model_validate(raw_state)


class A2AEnvelope(BaseModel):
    sender_runtime_id: str = Field(min_length=3, max_length=64)
    sender_agent_name: str | None = None
    target_runtime_id: str = Field(min_length=3, max_length=64)
    conversation_id: str = Field(min_length=1)
    message_id: str = Field(default_factory=lambda: uuid4().hex)
    reply_to_message_id: str | None = None
    intent: A2AIntent = A2AIntent.ASK
    text: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    attachments: tuple[A2AAttachment, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def normalize_values(self) -> Self:
        self.sender_runtime_id = self.sender_runtime_id.strip()
        self.target_runtime_id = self.target_runtime_id.strip()
        self.conversation_id = self.conversation_id.strip()
        self.message_id = self.message_id.strip()
        self.text = self.text.strip()

        if self.sender_agent_name is not None:
            self.sender_agent_name = self.sender_agent_name.strip() or None

        if self.reply_to_message_id is not None:
            self.reply_to_message_id = self.reply_to_message_id.strip() or None

        for runtime_id, field_name in (
            (self.sender_runtime_id, "sender_runtime_id"),
            (self.target_runtime_id, "target_runtime_id"),
        ):
            if not _RUNTIME_ID_PATTERN.fullmatch(runtime_id):
                raise ValueError(
                    f"{field_name} must start with an alphanumeric character and only contain letters, numbers, '.', '_' or '-'"
                )

        if not self.conversation_id:
            raise ValueError("conversation_id must not be blank")

        if not self.message_id:
            raise ValueError("message_id must not be blank")

        if not self.text:
            raise ValueError("text must not be blank")

        return self

    @property
    def sender_target(self) -> A2ATarget:
        return A2ATarget(runtime_id=self.sender_runtime_id, conversation_id=self.conversation_id)

    @property
    def local_conversation_id(self) -> str:
        return self.sender_target.as_conversation_target()

    @property
    def convergence_state(self) -> A2AConvergenceState:
        return A2AConvergenceState.from_metadata(self.metadata)

    @property
    def origin_route(self) -> tuple[str, str] | None:
        if self.reply_to_message_id is None:
            return None

        if self.intent not in {A2AIntent.RESULT, A2AIntent.INFORM, A2AIntent.ERROR, A2AIntent.HEARTBEAT}:
            return None

        return extract_a2a_origin_route(self.metadata)

    def render_inbound_text(self) -> str:
        sender_label = self.sender_runtime_id
        if self.sender_agent_name:
            sender_label = f"{sender_label} ({self.sender_agent_name})"

        lines = [f"A2A {self.intent.value} from {sender_label}", self.text]
        convergence_state = self.convergence_state
        lines.append(
            f"Convergence: hop {convergence_state.hop_count}/{convergence_state.max_hops}; remaining automatic replies {convergence_state.remaining_hops}."
        )

        block_reason = convergence_state.reply_block_reason(self.intent)
        if block_reason == "terminal_intent":
            lines.append(
                "This A2A message is informational or terminal. Process it locally, but do not send an automatic reply on the same exchange."
            )
        elif block_reason == "convergence_limit":
            lines.append(convergence_state.render_limit_message())

        if self.origin_route is not None:
            lines.append(
                "If you want to answer the original requester, reply normally in this local A2A session. The runtime will route your final response back to the preserved origin channel automatically."
            )

        if self.attachments:
            attachment_summary = ", ".join(attachment.render_summary() for attachment in self.attachments[:5])
            if len(self.attachments) > 5:
                attachment_summary = f"{attachment_summary}, +{len(self.attachments) - 5} more"
            lines.append(f"Attachments: {attachment_summary}")

        return "\n\n".join(line for line in lines if line)

    def to_inbound_metadata(self) -> dict[str, Any]:
        envelope_payload = self.model_dump(mode="json")
        envelope_payload["local_conversation_id"] = self.local_conversation_id
        convergence_state = self.convergence_state
        inbound_metadata = {
            "source": "a2a",
            "a2a": envelope_payload,
            "a2a_intent": self.intent.value,
            "a2a_sender_runtime_id": self.sender_runtime_id,
            "a2a_target_runtime_id": self.target_runtime_id,
            "a2a_message_id": self.message_id,
            "a2a_reply_to_message_id": self.reply_to_message_id,
            _A2A_CONVERGENCE_METADATA_KEY: convergence_state.to_metadata(),
            "a2a_hop_count": convergence_state.hop_count,
            "a2a_max_hops": convergence_state.max_hops,
            "a2a_stop_requested": convergence_state.stop_requested,
            "a2a_stop_reason": convergence_state.stop_reason,
        }

        if origin_metadata := extract_a2a_origin_routing_metadata(self.metadata):
            inbound_metadata.update(origin_metadata)

        if origin_channel_metadata := extract_a2a_origin_channel_metadata(self.metadata):
            inbound_metadata[_A2A_ORIGIN_CHANNEL_METADATA_KEY] = origin_channel_metadata

        return inbound_metadata

    def to_inbound_message(
        self,
        *,
        channel_name: str = "a2a",
        extra_metadata: dict[str, Any] | None = None,
    ) -> InboundMessage:
        metadata = self.to_inbound_metadata()
        if extra_metadata:
            metadata.update(extra_metadata)

        return InboundMessage(
            channel_name=channel_name,
            conversation_id=self.local_conversation_id,
            text=self.render_inbound_text(),
            user_id=self.sender_runtime_id,
            message_id=self.message_id,
            metadata=metadata,
        )

    @classmethod
    def from_inbound_metadata(cls, metadata: dict[str, Any]) -> Self:
        raw_a2a = metadata.get("a2a")
        if not isinstance(raw_a2a, dict):
            raise ValueError("Inbound message does not include A2A envelope metadata")

        return cls.model_validate(raw_a2a)


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
    context: MessageProcessingContext | None = Field(
        default=None,
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
