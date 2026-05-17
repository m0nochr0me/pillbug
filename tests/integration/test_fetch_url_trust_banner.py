"""End-to-end tests for fetch_url trust banner + read_file provenance (plan P2 #13)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import mcp as mcp_mod
from app.core.config import settings


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path, monkeypatch):
    # The default Settings instance pins MCP_FETCH_URL_OUTPUT_DIR to the original
    # WORKSPACE_ROOT at construction time; redirect it at the isolated workspace so
    # fetch_url can write under the per-test sandbox.
    monkeypatch.setattr(settings, "MCP_FETCH_URL_OUTPUT_DIR", settings.WORKSPACE_ROOT / "fetched", raising=True)
    return settings


class _FakeContent:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def iter_chunked(self, chunk_size: int):
        for offset in range(0, len(self._payload), chunk_size):
            yield self._payload[offset : offset + chunk_size]


class _FakeResponse:
    def __init__(self, *, url: str, payload: bytes, content_type: str, charset: str | None = "utf-8"):
        self.url = url
        self.content = _FakeContent(payload)
        self.content_length = len(payload)
        self.content_type = content_type
        self.charset = charset
        self.status = 200

    def raise_for_status(self) -> None:  # pragma: no cover - never raises in tests
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url: str, allow_redirects: bool = True):
        # Capture the requested URL so the response can echo it as final_url when needed.
        self._response.url = self._response.url or url
        return self._response


def _patch_aiohttp(monkeypatch, response: _FakeResponse) -> None:
    session = _FakeSession(response)
    monkeypatch.setattr(mcp_mod.aiohttp, "ClientSession", lambda *args, **kwargs: session)


async def test_fetch_url_html_writes_banner_and_read_file_surfaces_provenance(monkeypatch, workspace_settings):
    response = _FakeResponse(
        url="https://example.com/article",
        payload=b"<html><body><article><p>Hello world body text.</p></article></body></html>",
        content_type="text/html",
    )
    _patch_aiohttp(monkeypatch, response)

    fetch_result = await mcp_mod.fetch_url("https://example.com/article")
    assert fetch_result["content_mode"] == "readable-html"

    saved_path = fetch_result["path"]
    on_disk = (settings.WORKSPACE_ROOT / saved_path).read_text(encoding="utf-8")
    assert on_disk.startswith("---\n")
    assert "trust: untrusted" in on_disk
    assert "source: https://example.com/article" in on_disk
    assert "content_mode: readable-html" in on_disk

    read_result = await mcp_mod.read_file(saved_path)
    assert "provenance" in read_result
    assert read_result["provenance"]["trust"] == "untrusted"
    assert read_result["provenance"]["source"] == "https://example.com/article"
    assert read_result["provenance"]["content_mode"] == "readable-html"


async def test_fetch_url_text_writes_inline_banner(monkeypatch, workspace_settings):
    response = _FakeResponse(
        url="https://example.com/notes.txt",
        payload=b"line one\nline two\n",
        content_type="text/plain",
    )
    _patch_aiohttp(monkeypatch, response)

    fetch_result = await mcp_mod.fetch_url("https://example.com/notes.txt")
    assert fetch_result["content_mode"] == "text"
    on_disk = (settings.WORKSPACE_ROOT / fetch_result["path"]).read_text(encoding="utf-8")
    assert on_disk.startswith("---\n")
    assert "content_mode: text" in on_disk
    assert on_disk.endswith("line one\nline two\n")


async def test_fetch_url_binary_writes_sidecar_metadata(monkeypatch, workspace_settings):
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    response = _FakeResponse(
        url="https://example.com/icon.png",
        payload=payload,
        content_type="image/png",
        charset=None,
    )
    _patch_aiohttp(monkeypatch, response)

    fetch_result = await mcp_mod.fetch_url("https://example.com/icon.png")
    assert fetch_result["content_mode"] == "binary"
    assert "provenance_sidecar" in fetch_result

    binary_path = settings.WORKSPACE_ROOT / fetch_result["path"]
    assert binary_path.read_bytes() == payload
    sidecar_path = binary_path.parent / f"{binary_path.name}.metadata.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["trust"] == "untrusted"
    assert sidecar["content_mode"] == "binary"
    assert sidecar["source"] == "https://example.com/icon.png"


async def test_read_file_without_banner_omits_provenance(workspace_settings):
    target = settings.WORKSPACE_ROOT / "plain.txt"
    target.write_text("nothing to see\n", encoding="utf-8")

    result = await mcp_mod.read_file("plain.txt")
    assert "provenance" not in result
    assert result["content"] == "nothing to see\n"
