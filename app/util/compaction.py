"""
Compaction helpers for MCP tool responses.
"""

import re
from collections.abc import Awaitable, Callable, Sequence

from app.core.url_shortener import local_url_shortener
from app.util.text import full_cleanup_text, slight_cleanup_text

__all__ = (
    "apply_compaction_stages",
    "build_compaction_stage",
    "validate_compaction_stages",
)

type CompactionStage = Callable[[str], Awaitable[str]]

_URL_PATTERN = re.compile(
    r"(?:(?:https?://)|(?:www\.))"
    r"[^\s<>'\"]+"
)
_REGEX_SUB_PREFIX = "rsub::"


def _normalize_stage(stage: str) -> str:
    normalized_stage = stage.strip()
    if not normalized_stage:
        raise ValueError("Compaction stage must not be empty")
    return normalized_stage


def _wrap_sync_stage(stage_function: Callable[[str], str]) -> CompactionStage:
    async def runner(text: str) -> str:
        return stage_function(text)

    return runner


async def _shorten_urls(text: str) -> str:
    urls: list[str] = []
    seen_urls: set[str] = set()

    for match in _URL_PATTERN.finditer(text):
        url = match.group(0)
        if url in seen_urls:
            continue

        seen_urls.add(url)
        urls.append(url)

    if not urls:
        return text

    shortened_urls = await local_url_shortener.shorten_many(tuple(urls))
    return _URL_PATTERN.sub(
        lambda match: shortened_urls.get(match.group(0), match.group(0)),
        text,
    )


def _build_regex_sub_stage(stage: str) -> CompactionStage:
    parts = stage.split("::", maxsplit=2)
    if len(parts) != 3 or parts[0] != "rsub":
        raise ValueError("Regex substitution stages must use the format rsub::<pattern>::<replacement>")

    _, pattern, replacement = parts
    if not pattern:
        raise ValueError("Regex substitution pattern must not be empty")

    try:
        regex = re.compile(pattern, flags=re.DOTALL)
    except re.error as exc:
        raise ValueError(f"Invalid regex substitution pattern: {exc}") from exc

    async def runner(text: str) -> str:
        return regex.sub(replacement, text)

    return runner


def build_compaction_stage(stage: str) -> CompactionStage:
    normalized_stage = _normalize_stage(stage)

    if normalized_stage == "slight":
        return _wrap_sync_stage(slight_cleanup_text)

    if normalized_stage == "full":
        return _wrap_sync_stage(full_cleanup_text)

    if normalized_stage == "url_shorten":
        return _shorten_urls

    if normalized_stage.startswith(_REGEX_SUB_PREFIX):
        return _build_regex_sub_stage(normalized_stage)

    raise ValueError(
        "Unsupported compaction stage. Supported stages are slight, full, "
        "url_shorten, and rsub::<pattern>::<replacement>"
    )


async def apply_compaction_stages(text: str, stages: Sequence[str]) -> str:
    compacted = text
    for stage in stages:
        compacted = await build_compaction_stage(stage)(compacted)
    return compacted


def validate_compaction_stages(stages: Sequence[str] | None) -> list[str] | None:
    if stages is None:
        return None

    normalized_stages: list[str] = []
    for stage in stages:
        normalized_stage = _normalize_stage(stage)
        build_compaction_stage(normalized_stage)
        normalized_stages.append(normalized_stage)

    return normalized_stages
