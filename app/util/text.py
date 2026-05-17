"""
Text preprocessing utilities.
"""

import re
import unicodedata
from urllib.parse import unquote

__all__ = (
    "GOODBYE_MARKER",
    "SILENCE_MARKER",
    "classify_shell_stderr",
    "convert_ellipsis",
    "full_cleanup_text",
    "redact_secrets",
    "slight_cleanup_text",
)

_SECRET_KEY_PATTERN = re.compile(
    r"""((?:api[_-]?key|token|secret|password|credential|authorization|bearer)["']?\s*[:=]\s*["']?)[^\s"',}\]]+""",
    re.IGNORECASE,
)


def redact_secrets(text: str) -> str:
    """Replace likely secret values in `text` with `[REDACTED]`.

    Targets common JSON / kwarg shapes (`"api_key": "abc..."`, `token=xyz`) without
    requiring strict JSON parsing. Designed for short audit summaries (plan P2 #16)
    where best-effort redaction is preferable to leaking a secret into telemetry.
    """
    return _SECRET_KEY_PATTERN.sub(r"\1[REDACTED]", text)


_SHELL_STDERR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("command_not_found", re.compile(r"command not found|: not found", re.IGNORECASE)),
    ("permission_denied", re.compile(r"permission denied", re.IGNORECASE)),
    ("no_such_file_or_directory", re.compile(r"no such file or directory", re.IGNORECASE)),
)


def classify_shell_stderr(stderr: str) -> str | None:
    """Map common shell stderr fragments onto a small taxonomy of well-known failures.

    Returns one of `command_not_found`, `permission_denied`, `no_such_file_or_directory`,
    or None if no pattern matched. Inspection is capped at the first 200 characters of
    stderr to avoid being misled by long appended diagnostics.
    """

    if not stderr:
        return None
    sample = stderr[:200]
    for label, pattern in _SHELL_STDERR_PATTERNS:
        if pattern.search(sample):
            return label
    return None


SILENCE_MARKER: str = "…"

GOODBYE_MARKER = [
    # English
    "goodbye",
    # "bye",
    "see you",
    "take care",
    "farewell",
    "have a great day",
    "thank you for your time",
    # Russian
    "до свидания",
    # "пока",
    "увидимся",
    "береги себя",
    "прощай",
    "хорошего дня",
    "спасибо за уделенное время",
    # Kazakh
    "сау болыңыз",
    "көріскенше",
    "қош болыңыз",
    "жақсы күн тілеймін",
    "уақытыңызды бөлгеніңіз үшін рахмет",
]


def convert_ellipsis(text: str) -> str:
    """Convert three consecutive dots to a single ellipsis character."""
    return text.replace("...", "…")


def deduplicate_whitespace(text: str) -> str:
    """Replace multiple whitespace characters with a single space."""
    return re.sub(r"\s{3,}", " ", text).strip()


def slight_cleanup_text(text: str) -> str:
    """
    Cleans up the text by normalizing Unicode characters and unquoting URL-encoded strings and normalizing whitespace.
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join([c for c in text if not unicodedata.combining(c)])
    text = unquote(text)
    text = deduplicate_whitespace(text)
    return text.strip()


def full_cleanup_text(text: str) -> str:
    """
    Preprocesses the text by removing unwanted characters.
    """
    text = slight_cleanup_text(text)
    text = re.sub(r"[!\"#$%&'()*+,-./:;<=>?@\[\\\]^_`{|}~]", "", text)
    return text.strip()
