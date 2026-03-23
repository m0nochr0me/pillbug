"""A2A channel plugin for Pillbug."""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import aiohttp
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from app.core.config import settings
from app.core.log import logger
from app.runtime.channels import BaseChannel
from app.schema.messages import (
    A2AConvergenceState,
    A2AEnvelope,
    A2AIntent,
    A2ATarget,
    InboundMessage,
    extract_a2a_origin_routing_metadata,
)

_DEFAULT_INGRESS_PATH = "/a2a/messages"


def _normalize_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized:
        raise ValueError("base_url must not be blank")

    if not normalized.startswith(("http://", "https://")):
        raise ValueError("base_url must start with http:// or https://")

    return normalized


def _normalize_ingress_path(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("ingress_path must not be blank")

    if not normalized.startswith("/"):
        normalized = f"/{normalized}"

    return normalized.rstrip("/") or _DEFAULT_INGRESS_PATH


def _parse_peers_json(raw_value: str) -> list[dict[str, Any]]:
    try:
        decoded = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError("PB_A2A_PEERS_JSON must be valid JSON") from exc

    if isinstance(decoded, list):
        return [item for item in decoded if isinstance(item, dict)]

    if isinstance(decoded, dict):
        peers: list[dict[str, Any]] = []
        for runtime_id, payload in decoded.items():
            if isinstance(payload, str):
                peers.append({"runtime_id": runtime_id, "base_url": payload})
                continue

            if isinstance(payload, dict):
                peers.append({"runtime_id": runtime_id, **payload})
                continue

            raise ValueError("PB_A2A_PEERS_JSON mapping values must be strings or objects")

        return peers

    raise ValueError("PB_A2A_PEERS_JSON must decode to a list or object")


class A2APeerConfig(BaseModel):
    runtime_id: str = Field(min_length=3, max_length=64)
    base_url: str
    bearer_token: SecretStr | None = None

    @field_validator("runtime_id")
    @classmethod
    def validate_runtime_id(cls, value: str) -> str:
        return A2ATarget(runtime_id=value, conversation_id="bootstrap").runtime_id

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return _normalize_base_url(value)


class A2AChannelSettings(BaseModel):
    self_base_url: str | None = None
    ingress_path: str = _DEFAULT_INGRESS_PATH
    outbound_timeout_seconds: float = 15.0
    convergence_max_hops: int = 2
    peers: tuple[A2APeerConfig, ...] = Field(default_factory=tuple)

    @field_validator("self_base_url")
    @classmethod
    def validate_self_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_base_url(value)

    @field_validator("ingress_path")
    @classmethod
    def validate_ingress_path(cls, value: str) -> str:
        return _normalize_ingress_path(value)

    @field_validator("outbound_timeout_seconds")
    @classmethod
    def validate_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("outbound_timeout_seconds must be greater than zero")
        return value

    @field_validator("convergence_max_hops")
    @classmethod
    def validate_convergence_max_hops(cls, value: int) -> int:
        if value < 1:
            raise ValueError("convergence_max_hops must be greater than zero")
        return value

    @model_validator(mode="after")
    def validate_unique_peers(self) -> A2AChannelSettings:
        peer_ids = [peer.runtime_id for peer in self.peers]
        if len(peer_ids) != len(set(peer_ids)):
            raise ValueError("A2A peer runtime ids must be unique")
        return self

    @classmethod
    def from_env(cls) -> A2AChannelSettings:
        raw_settings: dict[str, Any] = {}

        if settings.A2A_SELF_BASE_URL:
            raw_settings["self_base_url"] = settings.A2A_SELF_BASE_URL

        raw_settings["ingress_path"] = settings.A2A_INGRESS_PATH
        raw_settings["outbound_timeout_seconds"] = settings.A2A_OUTBOUND_TIMEOUT_SECONDS
        raw_settings["convergence_max_hops"] = settings.A2A_CONVERGENCE_MAX_HOPS

        if raw_peers_json := os.environ.get("PB_A2A_PEERS_JSON"):
            raw_settings["peers"] = _parse_peers_json(raw_peers_json)

        return cls.model_validate(raw_settings)

    def peer_for(self, runtime_id: str) -> A2APeerConfig | None:
        for peer in self.peers:
            if peer.runtime_id == runtime_id:
                return peer
        return None


@dataclass(slots=True)
class A2AInboundPolicy:
    should_process: bool = True
    should_reply: bool = True
    reason: str | None = None
    reply_text: str | None = None


class A2AChannel(BaseChannel):
    name = "a2a"
    destination_kind = "runtime_id/conversation_id"

    def __init__(self, channel_settings: A2AChannelSettings | None = None) -> None:
        self._settings = channel_settings or A2AChannelSettings.from_env()
        self._inbound_queue: asyncio.Queue[InboundMessage | None] = asyncio.Queue()
        self._http_session: aiohttp.ClientSession | None = None
        self._closed = False

    async def listen(self) -> AsyncIterator[InboundMessage]:
        while True:
            inbound_message = await self._inbound_queue.get()
            if inbound_message is None:
                return

            yield inbound_message

    async def enqueue_envelope(
        self,
        envelope: A2AEnvelope,
        *,
        client_host: str | None = None,
    ) -> InboundMessage:
        if envelope.target_runtime_id != settings.runtime_id:
            raise ValueError(
                f"Envelope target_runtime_id {envelope.target_runtime_id!r} does not match runtime {settings.runtime_id!r}"
            )

        inbound_message = envelope.to_inbound_message(
            channel_name=self.name,
            extra_metadata={"a2a_client_host": client_host} if client_host else None,
        )
        await self._inbound_queue.put(inbound_message)
        logger.info(
            f"Queued A2A envelope from {envelope.sender_runtime_id} to {envelope.target_runtime_id} conversation={envelope.conversation_id}"
        )
        return inbound_message

    async def send_message(
        self,
        conversation_id: str,
        message_text: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        target = A2ATarget.parse(conversation_id)
        convergence_state = A2AConvergenceState(max_hops=self._settings.convergence_max_hops)
        envelope = A2AEnvelope(
            sender_runtime_id=settings.runtime_id,
            sender_agent_name=settings.AGENT_NAME,
            target_runtime_id=target.runtime_id,
            conversation_id=target.conversation_id,
            intent=A2AIntent.ASK,
            text=message_text,
            metadata=self._build_outbound_metadata(convergence_state=convergence_state, extra_metadata=metadata),
        )
        await self._deliver_envelope(target.runtime_id, envelope)

    async def send_response(
        self,
        inbound_message: InboundMessage,
        response_text: str,
    ) -> None:
        original_envelope = A2AEnvelope.from_inbound_metadata(inbound_message.metadata)
        fallback_base_url = self._fallback_sender_base_url(inbound_message)
        convergence_state = original_envelope.convergence_state.next_outbound()
        origin_metadata = extract_a2a_origin_routing_metadata(original_envelope.metadata)
        envelope = A2AEnvelope(
            sender_runtime_id=settings.runtime_id,
            sender_agent_name=settings.AGENT_NAME,
            target_runtime_id=original_envelope.sender_runtime_id,
            conversation_id=original_envelope.conversation_id,
            reply_to_message_id=original_envelope.message_id,
            intent=A2AIntent.RESULT,
            text=response_text,
            metadata=self._build_outbound_metadata(
                reply_to_message_id=original_envelope.message_id,
                convergence_state=convergence_state,
                extra_metadata=origin_metadata,
            ),
        )
        await self._deliver_envelope(
            original_envelope.sender_runtime_id,
            envelope,
            fallback_base_url=fallback_base_url,
        )

    def response_policy(self, inbound_message: InboundMessage) -> A2AInboundPolicy:
        envelope = A2AEnvelope.from_inbound_metadata(inbound_message.metadata)
        block_reason = envelope.convergence_state.reply_block_reason(envelope.intent)

        if block_reason == "terminal_intent":
            return A2AInboundPolicy(should_process=True, should_reply=False, reason=block_reason)

        if block_reason == "stop_requested":
            return A2AInboundPolicy(should_process=True, should_reply=False, reason=block_reason)

        if block_reason == "convergence_limit":
            return A2AInboundPolicy(
                should_process=False,
                should_reply=True,
                reason=block_reason,
                reply_text=envelope.convergence_state.render_limit_message(),
            )

        return A2AInboundPolicy()

    def context_destinations(self, known_destinations: tuple[str, ...]) -> tuple[str, ...]:
        observed_targets = [destination for destination in known_destinations if destination]
        observed_runtimes = {
            runtime_id.strip()
            for destination in observed_targets
            for runtime_id, separator, _conversation_id in (destination.partition("/"),)
            if separator and runtime_id.strip()
        }

        configured_placeholders = [
            f"{peer.runtime_id}/<conversation_id>"
            for peer in self._settings.peers
            if peer.runtime_id not in observed_runtimes
        ]

        return tuple(observed_targets + configured_placeholders)

    def instruction_context(self) -> dict[str, Any] | None:
        if not self._settings.peers:
            return None

        peers: list[dict[str, str]] = []
        for peer in self._settings.peers:
            peers.append(
                {
                    "runtime_id": peer.runtime_id,
                    "base_url": peer.base_url,
                    "send_target": f"a2a:{peer.runtime_id}/<conversation_id>",
                    "agent_card_url": f"{peer.base_url}/.well-known/agent-card.json",
                }
            )

        return {
            "convergence_max_hops": self._settings.convergence_max_hops,
            "peers": tuple(peers),
        }

    async def close(self) -> None:
        if self._closed:
            return

        self._closed = True
        await self._inbound_queue.put(None)
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

    def _build_outbound_metadata(
        self,
        *,
        reply_to_message_id: str | None = None,
        convergence_state: A2AConvergenceState | None = None,
        extra_metadata: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {"transport": "pillbug-a2a"}
        if self._settings.self_base_url is not None:
            metadata["sender_base_url"] = self._settings.self_base_url
            metadata["agent_card_url"] = self._agent_card_url()
        if reply_to_message_id is not None:
            metadata["reply_to_message_id"] = reply_to_message_id
        if convergence_state is not None:
            metadata["pillbug_convergence"] = convergence_state.to_metadata()
        if extra_metadata:
            metadata.update(extra_metadata)
        return metadata

    def _agent_card_url(self) -> str | None:
        if self._settings.self_base_url is None:
            return None

        return f"{self._settings.self_base_url}/.well-known/agent-card.json"

    def _fallback_sender_base_url(self, inbound_message: InboundMessage) -> str | None:
        raw_a2a = inbound_message.metadata.get("a2a")
        if not isinstance(raw_a2a, dict):
            return None

        raw_metadata = raw_a2a.get("metadata")
        if not isinstance(raw_metadata, dict):
            return None

        sender_base_url = raw_metadata.get("sender_base_url")
        if not isinstance(sender_base_url, str) or not sender_base_url.strip():
            return None

        return _normalize_base_url(sender_base_url)

    async def _deliver_envelope(
        self,
        target_runtime_id: str,
        envelope: A2AEnvelope,
        *,
        fallback_base_url: str | None = None,
    ) -> None:
        peer = self._settings.peer_for(target_runtime_id)
        base_url = peer.base_url if peer is not None else fallback_base_url
        if base_url is None:
            raise ValueError(f"No A2A peer configured for runtime_id={target_runtime_id}")

        url = f"{base_url}{self._settings.ingress_path}"
        headers = {"Content-Type": "application/json"}
        bearer_token = self._resolve_bearer_token(peer)
        if bearer_token is not None:
            headers["Authorization"] = f"Bearer {bearer_token}"

        session = await self._get_http_session()
        async with session.post(url, json=envelope.model_dump(mode="json"), headers=headers) as response:
            if response.status >= 400:
                response_text = (await response.text()).strip()
                raise RuntimeError(
                    f"A2A peer {target_runtime_id} rejected envelope with HTTP {response.status}: {response_text or '<empty>'}"
                )

        logger.info(
            f"Delivered A2A envelope to {target_runtime_id} conversation={envelope.conversation_id} intent={envelope.intent.value}"
        )

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            timeout = aiohttp.ClientTimeout(total=self._settings.outbound_timeout_seconds)
            self._http_session = aiohttp.ClientSession(timeout=timeout)

        return self._http_session

    def _resolve_bearer_token(self, peer: A2APeerConfig | None) -> str | None:
        if peer is not None and peer.bearer_token is not None:
            return peer.bearer_token.get_secret_value()

        return settings.a2a_bearer_token()


def create_channel() -> A2AChannel:
    return A2AChannel()
