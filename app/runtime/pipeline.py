import re
import unicodedata
from functools import cache
from pathlib import Path

from pydantic import BaseModel, ValidationError

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

_DEFAULT_SECURITY_PATTERNS_JSON = """{
    "warning_patterns": [
        {
            "name": "prompt-injection-language",
            "pattern": "(?i)\\b(?:ignore|disregard|forget|override)\\b.{0,40}\\b(?:previous|prior|above|earlier)\\b.{0,40}\\b(?:instructions?|prompts?|rules?|messages?)\\b"
        },
        {
            "name": "system-prompt-reference",
            "pattern": "(?i)\\b(?:system|developer|hidden|internal)\\s+prompt\\b"
        },
        {
            "name": "role-override-request",
            "pattern": "(?i)\\b(?:you are now|act as|pretend to be|roleplay as)\\b.{0,60}\\b(?:root|superuser|shell|terminal|jailbroken|unrestricted)\\b"
        },
        {
            "name": "tool-manipulation-request",
            "pattern": "(?i)\\b(?:tool|function|mcp|plugin)\\b.{0,60}\\b(?:override|bypass|ignore|disable|reveal|dump)\\b"
        },
        {
            "name": "script-tag",
            "pattern": "(?i)<script\\b"
        },
        {
            "name": "encoded-execution-payload",
            "pattern": "(?i)\\b(?:base64|powershell\\s+-enc|fromcharcode|eval\\s*\\()"
        }
    ],
    "block_patterns": [
        {
            "name": "credential-exfiltration-request",
            "reason": "request attempts to reveal secrets or credentials",
            "pattern": "(?i)\\b(?:show|print|reveal|dump|send|upload|copy|exfiltrat(?:e|ion)|leak)\\b.{0,80}\\b(?:api[_ -]?keys?|tokens?|secrets?|passwords?|credentials?|session(?:s)?|cookies?)\\b"
        },
        {
            "name": "sensitive-file-access-request",
            "reason": "request targets sensitive local files",
            "pattern": "(?i)\\b(?:read|cat|print|show|open|copy|upload|send|dump)\\b.{0,100}(?:\\.env(?:\\.[\\w.-]+)?|id_rsa|authorized_keys|\\.ssh|/etc/passwd|/etc/shadow|\\.git/config|\\.git-credentials)"
        },
        {
            "name": "destructive-shell-command",
            "reason": "request contains destructive shell instructions",
            "pattern": "(?i)(?:rm\\s+-rf\\s+/|sudo\\s+rm\\s+-rf\\b|dd\\s+if=/dev/zero\\b|mkfs(?:\\.\\w+)?\\b|chmod\\s+-R\\s+777\\s+/|fork\\s*bomb|:\\s*\\(\\)\\s*\\{\\s*:\\s*\\|\\s*:\\s*&\\s*\\};\\s*:)"
        },
        {
            "name": "reverse-shell-pattern",
            "reason": "request contains reverse-shell behavior",
            "pattern": "(?i)(?:nc\\s+-e\\s+/bin/(?:sh|bash)|bash\\s+-i\\b.{0,120}/dev/tcp/|python(?:3)?\\s+-c\\b.{0,120}\\b(?:socket|pty|subprocess)\\b)"
        },
        {
            "name": "metadata-credential-access",
            "reason": "request targets cloud metadata or credential services",
            "pattern": "(?i)(?:169\\.254\\.169\\.254|metadata\\.google\\.internal|latest/meta-data|gcp-metadata)"
        },
        {
            "name": "safeguard-bypass-request",
            "reason": "request asks to disable core security protections",
            "pattern": "(?i)\\b(?:disable|bypass|remove|turn\\s+off|override|ignore)\\b.{0,80}\\b(?:security\\s+checks?|guardrails?|safeguards?|restrictions?|sandbox|policy|policies)\\b"
        },
        {
            "name": "pipe-to-shell-execution",
            "reason": "request uses pipe-to-shell execution",
            "pattern": "(?i)(?:curl|wget)\\b.{0,80}\\|\\s*(?:sh|bash|zsh)\\b"
        }
    ]
}
"""


class SecurityPatternDefinition(BaseModel):
    name: str
    pattern: str
    reason: str | None = None


class SecurityPatternsConfig(BaseModel):
    warning_patterns: tuple[SecurityPatternDefinition, ...] = ()
    block_patterns: tuple[SecurityPatternDefinition, ...] = ()


_DEFAULT_SECURITY_PATTERNS = SecurityPatternsConfig.model_validate_json(_DEFAULT_SECURITY_PATTERNS_JSON)


def render_default_security_patterns() -> str:
    return _DEFAULT_SECURITY_PATTERNS.model_dump_json(indent=2) + "\n"


def ensure_security_patterns_file() -> None:
    settings.SECURITY_PATTERNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if settings.SECURITY_PATTERNS_PATH.is_file():
        return

    settings.SECURITY_PATTERNS_PATH.write_text(render_default_security_patterns(), encoding="utf-8")


def _compile_security_patterns_config(
    config: SecurityPatternsConfig,
    *,
    patterns_source: str,
) -> tuple[tuple[tuple[str, re.Pattern[str]], ...], tuple[tuple[str, str, re.Pattern[str]], ...]]:
    warning_patterns: list[tuple[str, re.Pattern[str]]] = []
    block_patterns: list[tuple[str, str, re.Pattern[str]]] = []

    for definition in config.warning_patterns:
        try:
            warning_patterns.append((definition.name, re.compile(definition.pattern)))
        except re.error as exc:
            logger.warning(
                f"Invalid warning security pattern {definition.name!r} in {patterns_source}: {exc}. Skipping entry."
            )

    for definition in config.block_patterns:
        try:
            block_patterns.append(
                (
                    definition.name,
                    definition.reason or definition.name,
                    re.compile(definition.pattern),
                )
            )
        except re.error as exc:
            logger.warning(
                f"Invalid block security pattern {definition.name!r} in {patterns_source}: {exc}. Skipping entry."
            )

    return tuple(warning_patterns), tuple(block_patterns)


@cache
def _load_security_patterns_from_disk(
    patterns_path: str,
    modified_at_ns: int,
) -> tuple[tuple[tuple[str, re.Pattern[str]], ...], tuple[tuple[str, str, re.Pattern[str]], ...]]:
    del modified_at_ns
    path = Path(patterns_path)

    try:
        config_text = path.read_text(encoding="utf-8")
        config = SecurityPatternsConfig.model_validate_json(config_text)
    except (OSError, ValidationError) as exc:
        logger.warning(f"Failed to load security patterns from {patterns_path}: {exc}. Using defaults.")
        config = _DEFAULT_SECURITY_PATTERNS

    return _compile_security_patterns_config(config, patterns_source=patterns_path)


def _get_security_patterns() -> tuple[tuple[tuple[str, re.Pattern[str]], ...], tuple[tuple[str, str, re.Pattern[str]], ...]]:
    ensure_security_patterns_file()

    try:
        modified_at_ns = settings.SECURITY_PATTERNS_PATH.stat().st_mtime_ns
    except OSError as exc:
        logger.warning(
            f"Failed to stat security patterns file {settings.SECURITY_PATTERNS_PATH}: {exc}. Using defaults."
        )
        return _compile_security_patterns_config(
            _DEFAULT_SECURITY_PATTERNS,
            patterns_source="built-in defaults",
        )

    return _load_security_patterns_from_disk(str(settings.SECURITY_PATTERNS_PATH), modified_at_ns)


def _security_pattern_inputs(*, raw_text: str, cleaned_text: str, normalized_text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(text for text in (raw_text, cleaned_text, normalized_text) if text))


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
        warning_patterns, block_patterns = _get_security_patterns()
        pattern_inputs = _security_pattern_inputs(
            raw_text=state.batch.raw_text,
            cleaned_text=state.cleaned_text,
            normalized_text=state.normalized_text,
        )

        if not state.cleaned_text:
            reasons.append("message is empty after cleanup")

        if len(state.cleaned_text) > settings.INBOUND_MAX_MESSAGE_CHARS:
            reasons.append(
                f"message exceeds max length of {settings.INBOUND_MAX_MESSAGE_CHARS} characters after cleanup"
            )

        if _has_disallowed_control_characters(state.batch.raw_text):
            reasons.append("message contains disallowed control characters")

        for warning_name, pattern in warning_patterns:
            if any(pattern.search(candidate) for candidate in pattern_inputs) and warning_name not in warnings:
                warnings.append(warning_name)

        for _pattern_name, reason, pattern in block_patterns:
            if any(pattern.search(candidate) for candidate in pattern_inputs) and reason not in reasons:
                reasons.append(reason)

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
        state.context = MessageProcessingContext(
            channel=batch.channel_name,
            conversation_id=batch.conversation_id,
            user_id=batch.user_id,
            received_at=batch.received_at,
            debounced_message_count=batch.message_count,
            security_warnings=list(state.security.warnings),
            metadata=batch.last_message.metadata,
            normalized_text=state.normalized_text,
            model_input=(  # Compact extra context
                f"---\nchannel: {batch.channel_name}\n"
                f"user_id: {batch.user_id}\n"
                f"received_at: {batch.received_at.isoformat()}"
                f"{'\nsecurity_warnings: ' + ', '.join(state.security.warnings) if state.security.warnings else ''}\n"
                f"---\n\n{state.cleaned_text}"
            ),
        )

        return state


class InboundProcessingPipeline:
    def __init__(self) -> None:
        self._steps = (
            CleanupStep(),
            SecurityCheckStep(),
            ContextEnrichmentStep(),
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
