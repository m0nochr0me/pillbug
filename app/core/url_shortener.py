"""
Local URL shortener for compacted MCP responses.
"""

import asyncio
import base64
import hashlib
from pathlib import Path
from typing import Final

from app.core.config import settings
from app.core.log import logger
from app.schema.url_shortener import ShortUrlRecord, ShortUrlStore
from app.util.workspace import async_read_text_file, async_write_text_file

__all__ = ("LocalUrlShortener", "local_url_shortener")

_HASH_ENCODING: Final[str] = "ascii"
_URL_ENCODING: Final[str] = "utf-8"


class LocalUrlShortener:
    def __init__(self, store_path: Path | None = None) -> None:
        self._store_path = store_path or settings.MCP_SHORTENER_STORE_PATH
        self._records_by_token: dict[str, ShortUrlRecord] = {}
        self._tokens_by_url: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._startup_lock = asyncio.Lock()
        self._loaded = False

    async def shorten(self, url: str) -> str:
        normalized_url = url.strip()
        if not normalized_url:
            raise ValueError("url must not be empty")

        shortened_urls = await self.shorten_many((normalized_url,))
        return shortened_urls[normalized_url]

    async def shorten_many(self, urls: tuple[str, ...] | list[str]) -> dict[str, str]:
        unique_urls: list[str] = []
        seen_urls: set[str] = set()

        for url in urls:
            normalized_url = url.strip()
            if not normalized_url or normalized_url in seen_urls:
                continue

            seen_urls.add(normalized_url)
            unique_urls.append(normalized_url)

        if not unique_urls:
            return {}

        await self._ensure_loaded()

        async with self._lock:
            shortened_urls: dict[str, str] = {}
            created_count = 0

            for url in unique_urls:
                token = self._tokens_by_url.get(url)
                if token is None:
                    token = self._allocate_token(url)
                    self._records_by_token[token] = ShortUrlRecord(token=token, url=url)
                    self._tokens_by_url[url] = token
                    created_count += 1

                shortened_urls[url] = self._build_short_url(token)

            if created_count:
                await self._persist_locked()
                logger.info(f"Stored {created_count} new short URL mappings in {self._store_path}")

            return shortened_urls

    async def resolve(self, token: str) -> str | None:
        normalized_token = token.strip()
        if not normalized_token:
            return None

        await self._ensure_loaded()

        async with self._lock:
            record = self._records_by_token.get(normalized_token)
            return record.url if record is not None else None

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return

        async with self._startup_lock:
            if self._loaded:
                return

            await self._load_store()
            self._loaded = True

    async def _load_store(self) -> None:
        if not await asyncio.to_thread(self._store_path.is_file):
            async with self._lock:
                self._records_by_token = {}
                self._tokens_by_url = {}
            return

        raw_store = await async_read_text_file(self._store_path)
        store = ShortUrlStore.model_validate_json(raw_store)

        async with self._lock:
            self._records_by_token = {record.token: record for record in store.urls}
            self._tokens_by_url = {record.url: record.token for record in store.urls}

    async def _persist_locked(self) -> None:
        store = ShortUrlStore(urls=sorted(self._records_by_token.values(), key=lambda record: record.created_at))
        payload = store.model_dump_json(indent=2)
        await asyncio.to_thread(self._store_path.parent.mkdir, parents=True, exist_ok=True)
        await async_write_text_file(self._store_path, payload, mode="w")

    def _allocate_token(self, url: str) -> str:
        digest = base64.urlsafe_b64encode(hashlib.sha256(url.encode(_URL_ENCODING)).digest())
        encoded_digest = digest.decode(_HASH_ENCODING).rstrip("=")
        minimum_length = max(settings.MCP_SHORTENER_TOKEN_LENGTH, 1)

        for token_length in range(minimum_length, len(encoded_digest) + 1):
            token = encoded_digest[:token_length]
            existing_record = self._records_by_token.get(token)
            if existing_record is None or existing_record.url == url:
                return token

        raise RuntimeError("Unable to allocate a unique short URL token")

    def _build_short_url(self, token: str) -> str:
        return f"{settings.mcp_shortener_base_url()}{settings.mcp_shortener_route_prefix()}/{token}"


local_url_shortener = LocalUrlShortener()
