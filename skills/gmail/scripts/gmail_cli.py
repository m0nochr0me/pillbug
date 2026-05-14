#!/usr/bin/env python3
"""Gmail read-only CLI helper for the Pillbug `gmail` skill.

Authenticates with a Google service account that has domain-wide delegation,
impersonates the target address, and prints JSON to stdout.

Usage:
    gmail_cli.py list <address> [--max N] [--query "GMAIL_QUERY"] [--unread-only]
    gmail_cli.py get  <address> <message-id>

Exit codes: 0 ok, 2 usage, 3 auth/config, 4 API error.
"""

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HEADER_KEYS = ("from", "to", "cc", "bcc", "subject", "date", "reply-to", "message-id")


def _die(code: int, message: str) -> None:
    json.dump({"error": message}, sys.stderr)
    sys.stderr.write("\n")
    sys.exit(code)


def _resolve_service_account_path() -> Path:
    env_value = os.environ.get("PB_GMAIL_SERVICE_ACCOUNT_PATH")
    base_dir = Path(os.environ.get("PB_BASE_DIR") or Path.home() / ".pillbug")
    if env_value:
        candidate = Path(env_value)
        if not candidate.is_absolute():
            candidate = base_dir / candidate
    else:
        candidate = base_dir / "gmail_service_account.json"
    if not candidate.is_file():
        _die(3, f"service account file not found at {candidate}")
    return candidate


def _build_service(address: str):
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:
        _die(3, f"missing dependency: {exc}. Install with `uv sync --extra gmail`.")

    sa_path = _resolve_service_account_path()
    try:
        creds = service_account.Credentials.from_service_account_file(str(sa_path), scopes=SCOPES, subject=address)
    except Exception as exc:  # noqa: BLE001 — surface any auth error as exit 3
        _die(3, f"failed to load service account credentials: {exc}")

    try:
        return build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as exc:  # noqa: BLE001
        _die(3, f"failed to build Gmail client: {exc}")


def _headers_to_dict(payload_headers: list[dict[str, str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for header in payload_headers:
        name = header.get("name", "").lower()
        if name in HEADER_KEYS:
            result[name] = header.get("value", "")
    return result


def _decode_body_data(data: str | None) -> str | None:
    if not data:
        return None
    try:
        return base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None


def _walk_parts(part: dict[str, Any], bodies: dict[str, str | None], attachments: list[dict[str, Any]]) -> None:
    mime_type = part.get("mimeType", "")
    filename = part.get("filename") or ""
    body = part.get("body") or {}
    attachment_id = body.get("attachmentId")

    if filename and attachment_id:
        attachments.append(
            {
                "id": attachment_id,
                "filename": filename,
                "mime_type": mime_type,
                "size": body.get("size", 0),
            }
        )
    elif mime_type == "text/plain" and bodies["body_text"] is None:
        bodies["body_text"] = _decode_body_data(body.get("data"))
    elif mime_type == "text/html" and bodies["body_html"] is None:
        bodies["body_html"] = _decode_body_data(body.get("data"))

    for sub in part.get("parts", []) or []:
        _walk_parts(sub, bodies, attachments)


def _summarize_message(message: dict[str, Any]) -> dict[str, Any]:
    payload = message.get("payload") or {}
    headers = _headers_to_dict(payload.get("headers") or [])
    labels = message.get("labelIds") or []
    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "snippet": message.get("snippet", ""),
        "unread": "UNREAD" in labels,
    }


def cmd_list(args: argparse.Namespace) -> int:
    service = _build_service(args.address)

    query_parts: list[str] = []
    if args.query:
        query_parts.append(args.query)
    if args.unread_only:
        query_parts.append("is:unread")
    q = " ".join(query_parts) if query_parts else None

    try:
        list_response = service.users().messages().list(userId=args.address, maxResults=args.max, q=q).execute()
    except Exception as exc:  # noqa: BLE001
        _die(4, f"Gmail list failed: {exc}")

    ids = [item["id"] for item in list_response.get("messages", [])]
    messages: list[dict[str, Any]] = []
    for message_id in ids:
        try:
            message = (
                service.users()
                .messages()
                .get(
                    userId=args.address,
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "To", "Subject", "Date"],
                )
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            _die(4, f"Gmail get failed for {message_id}: {exc}")
        messages.append(_summarize_message(message))

    json.dump({"address": args.address, "count": len(messages), "messages": messages}, sys.stdout)
    sys.stdout.write("\n")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    service = _build_service(args.address)
    try:
        message = service.users().messages().get(userId=args.address, id=args.message_id, format="full").execute()
    except Exception as exc:  # noqa: BLE001
        _die(4, f"Gmail get failed: {exc}")

    payload = message.get("payload") or {}
    headers = _headers_to_dict(payload.get("headers") or [])
    bodies: dict[str, str | None] = {"body_text": None, "body_html": None}
    attachments: list[dict[str, Any]] = []
    _walk_parts(payload, bodies, attachments)

    output = {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "labels": message.get("labelIds") or [],
        "headers": headers,
        "body_text": bodies["body_text"],
        "body_html": bodies["body_html"],
        "attachments": attachments,
    }
    json.dump(output, sys.stdout)
    sys.stdout.write("\n")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gmail_cli.py", description=__doc__.splitlines()[0] if __doc__ else "")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list messages in a mailbox")
    list_parser.add_argument("address", help="mailbox to impersonate and read")
    list_parser.add_argument("--max", type=int, default=25, help="maximum messages to return (default 25)")
    list_parser.add_argument("--query", default=None, help="Gmail search query (e.g. 'from:boss@x.com newer_than:7d')")
    list_parser.add_argument("--unread-only", action="store_true", help="restrict to unread messages")
    list_parser.set_defaults(func=cmd_list)

    get_parser = subparsers.add_parser("get", help="fetch a single message by id")
    get_parser.add_argument("address", help="mailbox to impersonate and read")
    get_parser.add_argument("message_id", help="Gmail message id (hex)")
    get_parser.set_defaults(func=cmd_get)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "max", None) is not None and args.max < 1:
        _die(2, "--max must be at least 1")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
