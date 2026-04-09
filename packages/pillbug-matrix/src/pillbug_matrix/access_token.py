"""CLI utility for obtaining Matrix access tokens for Pillbug."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import shlex
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from nio import AsyncClient, LoginResponse

DEFAULT_HOMESERVER = "https://matrix.example.org"
DEFAULT_USER_ID = "@pillbug:example.org"
DEFAULT_DEVICE_NAME = "pillbug-matrix"


@dataclass(slots=True)
class AccessTokenRequest:
    homeserver_url: str
    user_id: str
    password: str
    device_name: str
    output_format: str


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Log in to Matrix once and print the access token values needed by Pillbug.",
    )
    parser.add_argument(
        "--homeserver",
        help="Matrix homeserver URL, for example https://matrix.example.org",
    )
    parser.add_argument(
        "--user-id",
        help="Full Matrix user ID, for example @pillbug:example.org",
    )
    parser.add_argument(
        "--password",
        help="Matrix password. If omitted, the command prompts for it.",
    )
    parser.add_argument(
        "--device-name",
        default=DEFAULT_DEVICE_NAME,
        help=f"Human-readable device name used during login. Defaults to {DEFAULT_DEVICE_NAME}.",
    )
    parser.add_argument(
        "--format",
        choices=("env", "json"),
        default="env",
        help="Output format. Use 'env' for shell exports or 'json' for machine-readable output.",
    )
    return parser


def _prompt_value(prompt: str, *, default: str | None = None, secret: bool = False) -> str:
    display_prompt = prompt if default is None else f"{prompt} [{default}]"
    display_prompt = f"{display_prompt}: "

    if secret:
        value = getpass.getpass(display_prompt)
    else:
        value = input(display_prompt)

    resolved = value.strip()
    if resolved:
        return resolved
    return default or ""


def _normalize_homeserver_url(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("Homeserver URL must not be empty")
    if not stripped.startswith(("https://", "http://")):
        stripped = f"https://{stripped}"
    return stripped.rstrip("/")


def _normalize_user_id(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("User ID must not be empty")
    return stripped


def _resolve_request(argv: Sequence[str] | None = None) -> AccessTokenRequest:
    args = _build_parser().parse_args(argv)

    homeserver_url = _normalize_homeserver_url(
        args.homeserver or _prompt_value("Homeserver URL", default=DEFAULT_HOMESERVER)
    )
    user_id = _normalize_user_id(args.user_id or _prompt_value("Full Matrix user ID", default=DEFAULT_USER_ID))
    password = args.password or _prompt_value("Matrix password", secret=True)
    if not password:
        raise ValueError("Password must not be empty")

    device_name = args.device_name.strip() or DEFAULT_DEVICE_NAME
    return AccessTokenRequest(
        homeserver_url=homeserver_url,
        user_id=user_id,
        password=password,
        device_name=device_name,
        output_format=args.format,
    )


async def _login(request: AccessTokenRequest) -> dict[str, str]:
    client = AsyncClient(request.homeserver_url, request.user_id)
    try:
        response = await client.login(request.password, device_name=request.device_name)
    finally:
        await client.close()

    if not isinstance(response, LoginResponse):
        message = getattr(response, "message", repr(response))
        raise RuntimeError(f"Matrix login failed: {message}")

    result = {
        "PB_MATRIX_HOMESERVER_URL": request.homeserver_url,
        "PB_MATRIX_USER_ID": response.user_id,
        "PB_MATRIX_ACCESS_TOKEN": response.access_token,
    }
    if response.device_id:
        result["PB_MATRIX_DEVICE_ID"] = response.device_id
    return result


def _render_output(payload: dict[str, str], *, output_format: str) -> str:
    if output_format == "json":
        return json.dumps(payload, indent=2, sort_keys=True)

    ordered_keys = (
        "PB_MATRIX_HOMESERVER_URL",
        "PB_MATRIX_USER_ID",
        "PB_MATRIX_DEVICE_ID",
        "PB_MATRIX_ACCESS_TOKEN",
    )
    lines = [f"export {key}={shlex.quote(payload[key])}" for key in ordered_keys if key in payload]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        request = _resolve_request(argv)
        print("Logging in to Matrix and requesting an access token...", file=sys.stderr)
        payload = asyncio.run(_login(request))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        "Login succeeded. Copy the values below into your environment or .env file.",
        file=sys.stderr,
    )
    print(_render_output(payload, output_format=request.output_format))
    return 0


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
