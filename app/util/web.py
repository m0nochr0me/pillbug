"""
Readable web-document extraction and fetch helpers.
"""

import hashlib
import mimetypes
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from app.util.compaction import apply_compaction_stages

__all__ = (
    "build_fetch_output_path",
    "decode_text_payload",
    "extract_readable_html",
    "looks_like_html",
    "looks_like_text",
    "parse_trust_banner",
    "render_readable_html_document",
    "render_trust_banner",
    "render_trust_banner_metadata",
)

TRUST_UNTRUSTED = "untrusted"
_TRUST_BANNER_FIELDS = (
    "source",
    "final_url",
    "fetched_at",
    "trust",
    "content_type",
    "content_mode",
)

_FILENAME_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
_POSITIVE_HINT_PATTERN = re.compile(
    r"\b(article|body|content|entry|main|page|post|prose|story|text)\b",
    flags=re.IGNORECASE,
)
_NEGATIVE_HINT_PATTERN = re.compile(
    r"\b(ad|ads|aside|banner|breadcrumb|comment|cookie|footer|header|menu|modal|nav|related|share|sidebar|"
    r"social|subscribe|toolbar)\b",
    flags=re.IGNORECASE,
)


class _ReadableHtmlParser(HTMLParser):
    _BLOCK_TAGS = frozenset(
        {
            "article",
            "blockquote",
            "dd",
            "div",
            "dl",
            "dt",
            "figcaption",
            "figure",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "hr",
            "li",
            "main",
            "ol",
            "p",
            "pre",
            "section",
            "table",
            "tr",
            "ul",
        }
    )
    _SKIP_TAGS = frozenset({"canvas", "iframe", "noscript", "script", "style", "svg", "template"})
    _NEGATIVE_TAGS = frozenset({"aside", "button", "dialog", "footer", "form", "header", "menu", "nav"})
    _POSITIVE_TAGS = frozenset({"article", "main"})

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._stack: list[tuple[str, bool, bool, bool]] = []
        self._skip_depth = 0
        self._negative_depth = 0
        self._positive_depth = 0
        self._body_fragments: list[str] = []
        self._focused_fragments: list[str] = []
        self._title_fragments: list[str] = []
        self._in_title = False
        self._link_stack: list[tuple[str | None, list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        attr_map = {name.lower(): value or "" for name, value in attrs}

        if normalized_tag == "title":
            self._in_title = True

        attr_text = " ".join(
            part
            for part in (attr_map.get("class"), attr_map.get("id"), attr_map.get("role"), attr_map.get("aria-label"))
            if part
        )
        positive = normalized_tag in self._POSITIVE_TAGS or attr_map.get("role", "").strip().lower() == "main"
        positive = positive or bool(_POSITIVE_HINT_PATTERN.search(attr_text))
        negative = normalized_tag in self._NEGATIVE_TAGS or bool(_NEGATIVE_HINT_PATTERN.search(attr_text))
        skip = normalized_tag in self._SKIP_TAGS
        positive = positive and not negative

        self._stack.append((normalized_tag, positive, negative, skip))

        if skip:
            self._skip_depth += 1
        if negative:
            self._negative_depth += 1
        if positive:
            self._positive_depth += 1

        if normalized_tag == "a":
            href = attr_map.get("href", "").strip() or None
            self._link_stack.append((href, []))
            return

        if normalized_tag == "img":
            alt_text = re.sub(r"\s+", " ", attr_map.get("alt", "")).strip()
            if alt_text:
                self._append_text(f"[Image: {alt_text}]")
            return

        if normalized_tag == "br":
            self._append_break()
        elif normalized_tag == "li":
            self._append_break(prefix="- ")

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()

        if normalized_tag == "title":
            self._in_title = False

        if normalized_tag == "a" and self._link_stack:
            href, link_text_parts = self._link_stack.pop()
            link_text = self._normalize_inline_text("".join(link_text_parts))
            resolved_href = urljoin(self._base_url, href) if href else ""

            if resolved_href and link_text and resolved_href != link_text:
                self._append_text(f"{link_text} ({resolved_href})")
            elif link_text:
                self._append_text(link_text)
            elif resolved_href:
                self._append_text(resolved_href)

        if normalized_tag in self._BLOCK_TAGS:
            self._append_break()

        self._pop_stack(normalized_tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_fragments.append(data)
            return

        normalized = self._normalize_inline_text(data)
        if not normalized or self._skip_depth:
            return

        if self._link_stack:
            self._link_stack[-1][1].append(f"{normalized} ")
            return

        self._append_text(normalized)

    def render(self) -> tuple[str | None, str]:
        body_text = self._normalize_output("".join(self._body_fragments))
        focused_text = self._normalize_output("".join(self._focused_fragments))
        title = self._normalize_output("".join(self._title_fragments)) or None

        if len(focused_text) >= max(400, len(body_text) // 5):
            return title, focused_text

        return title, body_text

    def _append_break(self, prefix: str = "") -> None:
        if self._skip_depth or self._negative_depth:
            return

        for target in self._targets():
            target.append("\n\n")
            if prefix:
                target.append(prefix)

    def _append_text(self, text: str) -> None:
        normalized = self._normalize_inline_text(text)
        if not normalized or self._skip_depth or self._negative_depth:
            return

        for target in self._targets():
            target.append(f"{normalized} ")

    def _targets(self) -> tuple[list[str], ...]:
        if self._positive_depth:
            return self._body_fragments, self._focused_fragments
        return (self._body_fragments,)

    def _pop_stack(self, tag: str) -> None:
        while self._stack:
            stack_tag, positive, negative, skip = self._stack.pop()
            if skip:
                self._skip_depth = max(self._skip_depth - 1, 0)
            if negative:
                self._negative_depth = max(self._negative_depth - 1, 0)
            if positive:
                self._positive_depth = max(self._positive_depth - 1, 0)
            if stack_tag == tag:
                return

    @staticmethod
    def _normalize_inline_text(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _normalize_output(text: str) -> str:
        compacted = re.sub(r"[ \t]+\n", "\n", text)
        compacted = re.sub(r"\n[ \t]+", "\n", compacted)
        compacted = re.sub(r"[ \t]{2,}", " ", compacted)
        compacted = re.sub(r"\s+([,.;:!?])", r"\1", compacted)
        compacted = re.sub(r"\n{3,}", "\n\n", compacted)
        return compacted.strip()


def _sanitize_filename(value: str, fallback: str) -> str:
    normalized = _FILENAME_SAFE_PATTERN.sub("-", value.strip().lower()).strip("-._")
    return normalized or fallback


def looks_like_html(content_type: str, url: str) -> bool:
    normalized_content_type = content_type.lower()
    if normalized_content_type in {"application/xhtml+xml", "text/html"}:
        return True

    return Path(urlparse(url).path).suffix.lower() in {".htm", ".html", ".xhtml"}


def looks_like_text(content_type: str, url: str) -> bool:
    normalized_content_type = content_type.lower()
    if normalized_content_type.startswith("text/"):
        return True

    if normalized_content_type in {
        "application/javascript",
        "application/json",
        "application/ld+json",
        "application/sql",
        "application/xml",
        "application/x-yaml",
        "application/yaml",
        "image/svg+xml",
    }:
        return True

    return Path(urlparse(url).path).suffix.lower() in {
        ".css",
        ".csv",
        ".js",
        ".json",
        ".md",
        ".rst",
        ".svg",
        ".toml",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }


def decode_text_payload(payload: bytes, charset: str | None) -> str:
    encodings = [charset, "utf-8", "utf-16", "latin-1"]
    for encoding in encodings:
        if not encoding:
            continue
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue

    return payload.decode("utf-8", errors="replace")


def _guess_fetch_extension(content_type: str, url: str, *, readable_html: bool) -> str:
    if readable_html:
        return ".md"

    guessed_extension = mimetypes.guess_extension(content_type.lower(), strict=False)
    if guessed_extension:
        return guessed_extension

    path_extension = Path(urlparse(url).path).suffix.lower()
    if path_extension:
        return path_extension

    if looks_like_text(content_type, url):
        return ".txt"

    return ".bin"


def build_fetch_output_path(url: str, content_type: str, output_dir: Path, *, readable_html: bool) -> Path:
    parsed_url = urlparse(url)
    host = _sanitize_filename(parsed_url.netloc or parsed_url.hostname or "resource", "resource")
    stem = _sanitize_filename(Path(parsed_url.path).stem or "index", "index")
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    extension = _guess_fetch_extension(content_type, url, readable_html=readable_html)
    return output_dir / f"{host}-{stem}-{digest}{extension}"


def render_readable_html_document(title: str | None, source_url: str, body: str) -> str:
    heading = title.strip() if title else "Web Page"
    normalized_body = body.strip()
    duplicate_heading_prefix = f"{heading}\n\n"
    if normalized_body == heading:
        normalized_body = ""
    elif normalized_body.startswith(duplicate_heading_prefix):
        normalized_body = normalized_body.removeprefix(duplicate_heading_prefix).lstrip()

    lines = [f"# {heading}", "", f"Source: {source_url}", "", normalized_body]
    return "\n".join(line for line in lines if line is not None).strip() + "\n"


def render_trust_banner_metadata(
    *,
    source_url: str,
    final_url: str,
    fetched_at: datetime,
    content_type: str,
    content_mode: str,
    trust: str = TRUST_UNTRUSTED,
) -> dict[str, Any]:
    return {
        "source": source_url,
        "final_url": final_url,
        "fetched_at": fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trust": trust,
        "content_type": content_type,
        "content_mode": content_mode,
    }


def render_trust_banner(
    *,
    source_url: str,
    final_url: str,
    fetched_at: datetime,
    content_type: str,
    content_mode: str,
    trust: str = TRUST_UNTRUSTED,
) -> str:
    metadata = render_trust_banner_metadata(
        source_url=source_url,
        final_url=final_url,
        fetched_at=fetched_at,
        content_type=content_type,
        content_mode=content_mode,
        trust=trust,
    )
    lines = ["---"]
    for field in _TRUST_BANNER_FIELDS:
        lines.append(f"{field}: {metadata[field]}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def parse_trust_banner(content: str) -> tuple[dict[str, str], str] | None:
    """Parse a leading YAML-frontmatter trust banner.

    Returns (provenance, remaining_content) when a recognizable banner is present at the
    very start of `content`. Recognition requires the opening `---`, a closing `---`, and
    a `trust:` field — to avoid mistaking unrelated frontmatter (e.g. SKILL.md) for a
    fetched-content trust banner.
    """
    if not content.startswith("---\n"):
        return None

    closing_index = content.find("\n---", 4)
    if closing_index == -1:
        return None

    header = content[4:closing_index]
    rest_start = closing_index + len("\n---")
    if rest_start < len(content) and content[rest_start] == "\n":
        rest_start += 1
    if rest_start < len(content) and content[rest_start] == "\n":
        rest_start += 1

    provenance: dict[str, str] = {}
    for line in header.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            return None
        provenance[key.strip()] = value.strip()

    if "trust" not in provenance:
        return None

    return provenance, content[rest_start:]


async def extract_readable_html(payload: bytes, final_url: str, charset: str | None) -> tuple[str | None, str]:
    html_text = decode_text_payload(payload, charset)
    parser = _ReadableHtmlParser(final_url)
    parser.feed(html_text)
    parser.close()

    title, readable_text = parser.render()
    if readable_text:
        readable_text = await apply_compaction_stages(readable_text, ("url_shorten",))
        return title, readable_text

    fallback_text = re.sub(r"<[^>]+>", " ", html_text)
    fallback_text = re.sub(r"\s+", " ", fallback_text).strip()
    fallback_text = await apply_compaction_stages(fallback_text, ("url_shorten",))
    return title, fallback_text
