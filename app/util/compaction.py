"""
Compaction helpers for MCP tool responses.
"""

import re
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

from app.util.text import full_cleanup_text, slight_cleanup_text

__all__ = (
    "apply_compaction_stages",
    "build_compaction_stage",
    "validate_compaction_stages",
)

type CompactionStage = Callable[[str], str]

_URL_PATTERN = re.compile(r"(?:(?:https?://)|(?:www\.))[^\s<>'\"]+")
_REGEX_SUB_PREFIX = "rsub::"
_MAX_URL_LENGTH = 64
_SHORTENED_URL_SUFFIX = "...[url shortened]"


def _normalize_stage(stage: str) -> str:
    normalized_stage = stage.strip()
    if not normalized_stage:
        raise ValueError("Compaction stage must not be empty")
    return normalized_stage


def _shorten_url(url: str, *, max_length: int = _MAX_URL_LENGTH) -> str:
    if len(url) <= max_length:
        return url

    parsed = urlsplit(url if "://" in url else f"https://{url}")
    origin = parsed.netloc or url.split("/", maxsplit=1)[0]
    prefix = f"{parsed.scheme}://{origin}" if parsed.scheme and parsed.netloc else origin

    budget = max_length - len(prefix) - len(_SHORTENED_URL_SUFFIX)
    if budget <= 1:
        return f"{prefix}{_SHORTENED_URL_SUFFIX}"

    remainder = (
        f"{parsed.path or ''}"
        f"{f'?{parsed.query}' if parsed.query else ''}"
        f"{f'#{parsed.fragment}' if parsed.fragment else ''}"
    )
    if not remainder:
        return f"{prefix}{_SHORTENED_URL_SUFFIX}"

    visible = remainder[: max(budget, 0)]
    return f"{prefix}{visible}{_SHORTENED_URL_SUFFIX}"


def _shorten_urls(text: str) -> str:
    return _URL_PATTERN.sub(lambda match: _shorten_url(match.group(0)), text)


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

    return lambda text: regex.sub(replacement, text)


def build_compaction_stage(stage: str) -> CompactionStage:
    normalized_stage = _normalize_stage(stage)

    if normalized_stage == "slight":
        return slight_cleanup_text

    if normalized_stage == "full":
        return full_cleanup_text

    if normalized_stage == "url_shorten":
        return _shorten_urls

    if normalized_stage.startswith(_REGEX_SUB_PREFIX):
        return _build_regex_sub_stage(normalized_stage)

    raise ValueError(
        "Unsupported compaction stage. Supported stages are slight, full, "
        "url_shorten, and rsub::<pattern>::<replacement>"
    )


def apply_compaction_stages(text: str, stages: Sequence[str]) -> str:
    compacted = text
    for stage in stages:
        compacted = build_compaction_stage(stage)(compacted)
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
