#!/usr/bin/env python3
"""Bluesky (ATProto) CLI helper for the `bluesky` skill.

Publishes posts to a user's Bluesky account via the ATProto XRPC API. Text-only
and text-with-images posts are supported. The client is intentionally small so
later extensions (replies, likes, feed reads) can reuse the same session.
"""

import argparse
import asyncio
import json
import mimetypes
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent


def load_dotenv() -> None:
    """Populate os.environ from the first `.env` found in cwd or skill dir."""
    for candidate in (Path.cwd() / ".env", SKILL_DIR / ".env"):
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
        return


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"error: {name} is not set (put it in .env or the environment)")
    return value


class BlueskyError(RuntimeError):
    pass


class BlueskyClient:
    def __init__(self, session: aiohttp.ClientSession, pds: str) -> None:
        self._session = session
        self._pds = pds.rstrip("/")
        self._access_jwt: str | None = None
        self._did: str | None = None

    @property
    def did(self) -> str:
        if self._did is None:
            raise BlueskyError("not logged in")
        return self._did

    def _auth_headers(self) -> dict[str, str]:
        if self._access_jwt is None:
            raise BlueskyError("not logged in")
        return {"Authorization": f"Bearer {self._access_jwt}"}

    async def _xrpc(
        self,
        method: str,
        nsid: str,
        *,
        json_body: dict[str, Any] | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        url = f"{self._pds}/xrpc/{nsid}"
        headers: dict[str, str] = {}
        if auth:
            headers.update(self._auth_headers())
        if content_type:
            headers["Content-Type"] = content_type

        kwargs: dict[str, Any] = {"headers": headers}
        if json_body is not None:
            kwargs["json"] = json_body
        if data is not None:
            kwargs["data"] = data

        async with self._session.request(method, url, **kwargs) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise BlueskyError(f"{nsid} failed ({resp.status}): {text}")
            return json.loads(text) if text else {}

    async def login(self, identifier: str, password: str) -> None:
        payload = await self._xrpc(
            "POST",
            "com.atproto.server.createSession",
            json_body={"identifier": identifier, "password": password},
            auth=False,
        )
        self._access_jwt = payload["accessJwt"]
        self._did = payload["did"]

    async def upload_blob(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise BlueskyError(f"image not found: {path}")
        mime, _ = mimetypes.guess_type(path.name)
        if mime is None or not mime.startswith("image/"):
            raise BlueskyError(f"could not determine image MIME type for {path}")
        data = path.read_bytes()
        result = await self._xrpc(
            "POST",
            "com.atproto.repo.uploadBlob",
            data=data,
            content_type=mime,
        )
        return result["blob"]

    async def create_record(self, collection: str, record: dict[str, Any]) -> dict[str, Any]:
        return await self._xrpc(
            "POST",
            "com.atproto.repo.createRecord",
            json_body={"repo": self.did, "collection": collection, "record": record},
        )


async def cmd_post(args: argparse.Namespace) -> int:
    handle = require_env("BSKY_HANDLE")
    password = require_env("BSKY_APP_PASSWORD")
    pds = os.environ.get("BSKY_PDS", "https://bsky.social")

    images: list[Path] = [Path(p) for p in (args.image or [])]
    alts: list[str] = list(args.alt or [])
    if len(images) > 4:
        sys.exit("error: Bluesky allows at most 4 images per post")
    if not args.text and not images:
        sys.exit("error: --text or at least one --image is required")

    async with aiohttp.ClientSession() as session:
        client = BlueskyClient(session, pds)
        await client.login(handle, password)

        record: dict[str, Any] = {
            "$type": "app.bsky.feed.post",
            "text": args.text or "",
            "createdAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }

        if images:
            embed_images = []
            for idx, img_path in enumerate(images):
                blob = await client.upload_blob(img_path)
                alt = alts[idx] if idx < len(alts) else ""
                embed_images.append({"alt": alt, "image": blob})
            record["embed"] = {
                "$type": "app.bsky.embed.images",
                "images": embed_images,
            }

        result = await client.create_record("app.bsky.feed.post", record)

    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bluesky",
        description="Bluesky (ATProto) CLI helper — publish posts with optional images.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    post = sub.add_parser("post", help="Create a new post on the authenticated account.")
    post.add_argument("--text", default="", help="Post text (may be empty if images are provided).")
    post.add_argument(
        "--image",
        action="append",
        help="Path to an image to attach. Repeat for up to 4 images.",
    )
    post.add_argument(
        "--alt",
        action="append",
        help="Alt text for the N-th --image (position-aligned).",
    )
    post.set_defaults(func=cmd_post)

    return parser


def main() -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(args.func(args))
    except BlueskyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
