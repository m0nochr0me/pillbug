"""Unit tests for fetch_url trust-banner render/parse helpers (plan P2 #13)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.util.web import (
    parse_trust_banner,
    render_trust_banner,
    render_trust_banner_metadata,
)


def _sample_kwargs():
    return {
        "source_url": "https://example.com/article",
        "final_url": "https://example.com/article",
        "fetched_at": datetime(2026, 5, 17, 12, 34, 56, tzinfo=UTC),
        "content_type": "text/html",
        "content_mode": "readable-html",
    }


def test_render_trust_banner_emits_yaml_frontmatter():
    banner = render_trust_banner(**_sample_kwargs())

    assert banner.startswith("---\n")
    assert banner.endswith("---\n\n")
    assert "source: https://example.com/article" in banner
    assert "final_url: https://example.com/article" in banner
    assert "fetched_at: 2026-05-17T12:34:56Z" in banner
    assert "trust: untrusted" in banner
    assert "content_type: text/html" in banner
    assert "content_mode: readable-html" in banner


def test_render_trust_banner_metadata_matches_banner_fields():
    metadata = render_trust_banner_metadata(**_sample_kwargs())

    assert metadata == {
        "source": "https://example.com/article",
        "final_url": "https://example.com/article",
        "fetched_at": "2026-05-17T12:34:56Z",
        "trust": "untrusted",
        "content_type": "text/html",
        "content_mode": "readable-html",
    }


def test_parse_trust_banner_round_trips():
    banner = render_trust_banner(**_sample_kwargs())
    document = banner + "# Hello\n\nBody.\n"

    parsed = parse_trust_banner(document)
    assert parsed is not None
    provenance, remainder = parsed
    assert provenance["trust"] == "untrusted"
    assert provenance["source"] == "https://example.com/article"
    assert remainder == "# Hello\n\nBody.\n"


def test_parse_trust_banner_returns_none_for_unrelated_frontmatter():
    # SKILL.md-style frontmatter has no `trust` field; must not be mistaken for a trust banner.
    skill_md = "---\nname: foo\ndescription: bar\n---\n\nBody.\n"
    assert parse_trust_banner(skill_md) is None


def test_parse_trust_banner_returns_none_when_no_frontmatter():
    assert parse_trust_banner("just text\n") is None
    assert parse_trust_banner("") is None


def test_parse_trust_banner_returns_none_when_unterminated():
    assert parse_trust_banner("---\ntrust: untrusted\nno closing fence\n") is None
