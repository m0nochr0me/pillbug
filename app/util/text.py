"""
Text preprocessing utilities.
"""

import re
import unicodedata
from urllib.parse import unquote

__all__ = (
    "GOODBYE_MARKER",
    "SILENCE_MARKER",
    "convert_ellipsis",
    "full_cleanup_text",
    "slight_cleanup_text",
)

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
    return re.sub(r"\s+", " ", text).strip()


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
