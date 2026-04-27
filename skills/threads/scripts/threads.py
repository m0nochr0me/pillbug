#!/usr/bin/env python3
"""Threads (Meta) CLI helper for the `threads` skill.

Publishes posts to a user's Threads account via the Threads Graph API.
Supports text-only posts, single image posts, and image carousels.

The OAuth user access token is obtained via `setup` and persisted to
`skills/threads/.credentials.json`. App credentials (id/secret) come from
the environment so they remain outside of disk-resident state.
"""

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
CREDENTIALS_PATH = SKILL_DIR / ".credentials.json"

THREADS_AUTH_URL = "https://threads.net/oauth/authorize"
THREADS_GRAPH_BASE = "https://graph.threads.net"
THREADS_API_VERSION = "v1.0"

DEFAULT_SCOPES = "threads_basic,threads_content_publish"
DEFAULT_POLL_TIMEOUT = 60
DEFAULT_POLL_INTERVAL = 2


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


class ThreadsError(RuntimeError):
    pass


def load_credentials() -> dict[str, Any]:
    if not CREDENTIALS_PATH.is_file():
        raise ThreadsError(f"credentials not found at {CREDENTIALS_PATH}; run `setup` first")
    return json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))


def save_credentials(data: dict[str, Any]) -> None:
    CREDENTIALS_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        CREDENTIALS_PATH.chmod(0o600)


class ThreadsClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._session.request(method, url, params=params, data=data) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise ThreadsError(f"{method} {url} failed ({resp.status}): {text}")
            return json.loads(text) if text else {}

    async def exchange_code(
        self,
        *,
        app_id: str,
        app_secret: str,
        redirect_uri: str,
        code: str,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"{THREADS_GRAPH_BASE}/oauth/access_token",
            data={
                "client_id": app_id,
                "client_secret": app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )

    async def exchange_for_long_lived(self, *, app_secret: str, short_token: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"{THREADS_GRAPH_BASE}/access_token",
            params={
                "grant_type": "th_exchange_token",
                "client_secret": app_secret,
                "access_token": short_token,
            },
        )

    async def refresh_long_lived(self, *, access_token: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"{THREADS_GRAPH_BASE}/refresh_access_token",
            params={"grant_type": "th_refresh_token", "access_token": access_token},
        )

    async def get_me(self, *, access_token: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"{THREADS_GRAPH_BASE}/{THREADS_API_VERSION}/me",
            params={"fields": "id,username", "access_token": access_token},
        )

    async def create_container(
        self,
        *,
        user_id: str,
        access_token: str,
        params: dict[str, Any],
    ) -> str:
        payload = {**params, "access_token": access_token}
        result = await self._request(
            "POST",
            f"{THREADS_GRAPH_BASE}/{THREADS_API_VERSION}/{user_id}/threads",
            data=payload,
        )
        container_id = result.get("id")
        if not container_id:
            raise ThreadsError(f"container creation returned no id: {result}")
        return container_id

    async def get_container_status(self, *, container_id: str, access_token: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"{THREADS_GRAPH_BASE}/{THREADS_API_VERSION}/{container_id}",
            params={"fields": "status,error_message", "access_token": access_token},
        )

    async def wait_for_container(
        self,
        *,
        container_id: str,
        access_token: str,
        timeout: int = DEFAULT_POLL_TIMEOUT,
        interval: int = DEFAULT_POLL_INTERVAL,
    ) -> None:
        deadline = time.monotonic() + timeout
        while True:
            status = await self.get_container_status(container_id=container_id, access_token=access_token)
            state = status.get("status")
            if state == "FINISHED":
                return
            if state == "ERROR":
                raise ThreadsError(f"container {container_id} failed: {status.get('error_message')}")
            if state == "EXPIRED":
                raise ThreadsError(f"container {container_id} expired before publishing")
            if time.monotonic() >= deadline:
                raise ThreadsError(f"container {container_id} not ready after {timeout}s (last status: {state})")
            await asyncio.sleep(interval)

    async def publish_container(
        self,
        *,
        user_id: str,
        access_token: str,
        container_id: str,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"{THREADS_GRAPH_BASE}/{THREADS_API_VERSION}/{user_id}/threads_publish",
            data={"creation_id": container_id, "access_token": access_token},
        )


def _build_auth_url(app_id: str, redirect_uri: str, scopes: str) -> str:
    qs = urlencode(
        {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "response_type": "code",
        }
    )
    return f"{THREADS_AUTH_URL}?{qs}"


def _extract_code(raw: str) -> str:
    """Accept either a bare code or a full callback URL pasted by the user."""
    raw = raw.strip()
    if not raw:
        raise ThreadsError("empty authorization code")
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        params = parse_qs(parsed.query)
        codes = params.get("code")
        if not codes:
            raise ThreadsError(f"no `code` parameter in callback URL: {raw}")
        raw = codes[0]
    # Threads short-lived auth codes are sometimes returned with a `#_` suffix.
    return raw.split("#")[0]


async def cmd_setup(args: argparse.Namespace) -> int:
    app_id = require_env("THREADS_APP_ID")
    app_secret = require_env("THREADS_APP_SECRET")
    redirect_uri = args.redirect_uri or os.environ.get("THREADS_REDIRECT_URI")
    if not redirect_uri:
        sys.exit("error: --redirect-uri or THREADS_REDIRECT_URI is required")
    scopes = args.scopes or os.environ.get("THREADS_SCOPES", DEFAULT_SCOPES)

    auth_url = _build_auth_url(app_id, redirect_uri, scopes)

    code_input = args.code
    if not code_input:
        print("Open this URL in a browser, authorize the app, then paste either")
        print("the full redirected URL or just the `code` query parameter:")
        print()
        print(f"  {auth_url}")
        print()
        try:
            code_input = input("code or callback URL: ")
        except EOFError:
            sys.exit("error: no authorization code provided")

    code = _extract_code(code_input)

    async with aiohttp.ClientSession() as session:
        client = ThreadsClient(session)
        short = await client.exchange_code(
            app_id=app_id,
            app_secret=app_secret,
            redirect_uri=redirect_uri,
            code=code,
        )
        short_token = short.get("access_token")
        user_id = short.get("user_id")
        if not short_token or not user_id:
            raise ThreadsError(f"unexpected token response: {short}")

        long = await client.exchange_for_long_lived(app_secret=app_secret, short_token=short_token)
        access_token = long.get("access_token")
        expires_in = long.get("expires_in")
        if not access_token:
            raise ThreadsError(f"unexpected long-lived token response: {long}")

        me = await client.get_me(access_token=access_token)

    obtained_at = int(time.time())
    credentials = {
        "user_id": str(user_id),
        "username": me.get("username"),
        "access_token": access_token,
        "token_type": long.get("token_type", "bearer"),
        "expires_in": expires_in,
        "obtained_at": obtained_at,
        "expires_at": obtained_at + int(expires_in) if expires_in else None,
        "scopes": scopes,
    }
    save_credentials(credentials)

    print(f"saved credentials for @{me.get('username')} (user_id={user_id}) to {CREDENTIALS_PATH}")
    return 0


async def cmd_refresh(args: argparse.Namespace) -> int:
    creds = load_credentials()
    async with aiohttp.ClientSession() as session:
        client = ThreadsClient(session)
        result = await client.refresh_long_lived(access_token=creds["access_token"])

    access_token = result.get("access_token")
    expires_in = result.get("expires_in")
    if not access_token:
        raise ThreadsError(f"unexpected refresh response: {result}")

    obtained_at = int(time.time())
    creds.update(
        {
            "access_token": access_token,
            "expires_in": expires_in,
            "obtained_at": obtained_at,
            "expires_at": obtained_at + int(expires_in) if expires_in else None,
        }
    )
    save_credentials(creds)
    print(f"refreshed access token; new expiry in {expires_in}s")
    return 0


async def _build_carousel(
    client: ThreadsClient,
    *,
    user_id: str,
    access_token: str,
    image_urls: list[str],
    alts: list[str],
) -> list[str]:
    container_ids: list[str] = []
    for idx, url in enumerate(image_urls):
        params: dict[str, Any] = {
            "media_type": "IMAGE",
            "image_url": url,
            "is_carousel_item": "true",
        }
        if idx < len(alts) and alts[idx]:
            params["alt_text"] = alts[idx]
        container_id = await client.create_container(user_id=user_id, access_token=access_token, params=params)
        container_ids.append(container_id)
    return container_ids


async def cmd_post(args: argparse.Namespace) -> int:
    creds = load_credentials()
    user_id = creds["user_id"]
    access_token = creds["access_token"]

    image_urls: list[str] = list(args.image_url or [])
    alts: list[str] = list(args.alt or [])

    if not args.text and not image_urls:
        sys.exit("error: --text or at least one --image-url is required")
    if len(image_urls) > 20:
        sys.exit("error: Threads allows at most 20 items per carousel")

    async with aiohttp.ClientSession() as session:
        client = ThreadsClient(session)

        if not image_urls:
            container_id = await client.create_container(
                user_id=user_id,
                access_token=access_token,
                params={"media_type": "TEXT", "text": args.text},
            )
        elif len(image_urls) == 1:
            params: dict[str, Any] = {
                "media_type": "IMAGE",
                "image_url": image_urls[0],
            }
            if args.text:
                params["text"] = args.text
            if alts and alts[0]:
                params["alt_text"] = alts[0]
            container_id = await client.create_container(user_id=user_id, access_token=access_token, params=params)
            await client.wait_for_container(container_id=container_id, access_token=access_token)
        else:
            child_ids = await _build_carousel(
                client,
                user_id=user_id,
                access_token=access_token,
                image_urls=image_urls,
                alts=alts,
            )
            for child_id in child_ids:
                await client.wait_for_container(container_id=child_id, access_token=access_token)
            params = {
                "media_type": "CAROUSEL",
                "children": ",".join(child_ids),
            }
            if args.text:
                params["text"] = args.text
            container_id = await client.create_container(user_id=user_id, access_token=access_token, params=params)
            await client.wait_for_container(container_id=container_id, access_token=access_token)

        result = await client.publish_container(user_id=user_id, access_token=access_token, container_id=container_id)

    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="threads",
        description="Threads (Meta) CLI helper — OAuth setup and post publishing.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser(
        "setup",
        help="Authorize the app and persist a long-lived access token.",
    )
    setup.add_argument(
        "--redirect-uri",
        help="OAuth redirect URI registered in the Meta app (overrides THREADS_REDIRECT_URI).",
    )
    setup.add_argument(
        "--code",
        help="Authorization code from the redirect URL. If omitted, the script prompts.",
    )
    setup.add_argument(
        "--scopes",
        help=f"Comma-separated scopes to request (default: {DEFAULT_SCOPES}).",
    )
    setup.set_defaults(func=cmd_setup)

    refresh = sub.add_parser(
        "refresh",
        help="Refresh the stored long-lived access token (extends expiry by ~60 days).",
    )
    refresh.set_defaults(func=cmd_refresh)

    post = sub.add_parser("post", help="Publish a new thread on the authenticated account.")
    post.add_argument("--text", default="", help="Post text (may be empty if images are provided).")
    post.add_argument(
        "--image-url",
        action="append",
        help="Public image URL to attach. Repeat for up to 20 carousel items.",
    )
    post.add_argument(
        "--alt",
        action="append",
        help="Alt text for the N-th --image-url (position-aligned).",
    )
    post.set_defaults(func=cmd_post)

    return parser


def main() -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(args.func(args))
    except ThreadsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
