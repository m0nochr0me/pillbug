---
name: gmail
description: Read Gmail mailboxes through a service account with domain-wide delegation. Use when the agent needs to list new emails for an address, fetch the full content of a particular message by id, or check for messages matching a Gmail search query. Read-only; sending and modification are out of scope. Requires service-account credentials and a Google Workspace domain with delegated access.
---

# Gmail

List and read Gmail messages via the bundled `gmail_cli.py` helper. Authentication uses a service account with domain-wide delegation; the helper impersonates the target mailbox.

## Usage

List recent messages for a mailbox (default max 25):

```bash
uv run python skills/gmail/scripts/gmail_cli.py list user@example.com
```

Restrict to unread, increase limit, or pass a Gmail search query:

```bash
uv run python skills/gmail/scripts/gmail_cli.py list user@example.com --unread-only --max 10
uv run python skills/gmail/scripts/gmail_cli.py list user@example.com --query "from:boss@x.com newer_than:7d"
```

Fetch the full content of a particular message:

```bash
uv run python skills/gmail/scripts/gmail_cli.py get user@example.com 18f3a2b1c4d5e6f7
```

## Output

JSON to stdout on success. On failure, a JSON `{"error": "..."}` to stderr and a non-zero exit code (2 usage, 3 auth/config, 4 Gmail API error).

`list` result:

```json
{"address": "user@example.com", "count": 3, "messages": [
  {"id": "...", "thread_id": "...", "from": "...", "to": "...",
   "subject": "...", "date": "...", "snippet": "...", "unread": true}
]}
```

`get` result:

```json
{"id": "...", "thread_id": "...", "labels": ["INBOX", "UNREAD"],
 "headers": {"from": "...", "to": "...", "cc": "...", "subject": "...", "date": "..."},
 "body_text": "...", "body_html": "..." | null,
 "attachments": [{"id": "...", "filename": "...", "mime_type": "...", "size": 1234}]}
```

Either `body_text` or `body_html` may be null if the message has only one MIME alternative. Attachment bytes are not fetched — only metadata.

## Environment

- `PB_GMAIL_SERVICE_ACCOUNT_PATH` — absolute or `PB_BASE_DIR`-relative path to the service account JSON. Defaults to `$PB_BASE_DIR/gmail_service_account.json` (typically `~/.pillbug/gmail_service_account.json`).
- `PB_BASE_DIR` — Pillbug runtime base dir. Defaults to `~/.pillbug`.

## Setup

Service account creation, domain-wide delegation, and scope authorization steps are in [references/auth-setup.md](./references/auth-setup.md). The service account must have the readonly Gmail scope authorized in the Workspace admin console, and the JSON key must be placed at the configured path.

## Notes

- Read-only scope (`gmail.readonly`). The helper cannot send, modify, or delete.
- The mailbox address is both the filter target *and* the impersonated subject — domain-wide delegation must be authorized for every address you intend to read.
- Install the optional dependency once with `uv sync --extra gmail`. Without it, the helper exits with code 3 and a clear error.
