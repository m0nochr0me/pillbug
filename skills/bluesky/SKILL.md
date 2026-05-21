---
name: bluesky
description: Publish posts to Bluesky. Use when the agent needs to create a Bluesky post.
---

# Bluesky

Publish posts to Bluesky from the CLI. The helper is a single-file Python script that talks to the ATProto XRPC endpoints directly.

Script: `scripts/bluesky.py`

## Environment

Each credential resolves from `/run/secrets/<name>` first — the name lowercased,
e.g. `BSKY_APP_PASSWORD` → `/run/secrets/bsky_app_password` — as provided by Docker
or Kubernetes secrets. When no secret file is present it falls back to the
environment or a `.env` file in the skill directory or current working directory.

| Variable            | Required | Purpose                                                           |
| ------------------- | -------- | ----------------------------------------------------------------- |
| `BSKY_HANDLE`       | yes      | Account handle, e.g. `name.bsky.social`.                          |
| `BSKY_APP_PASSWORD` | yes      | App password created in Bluesky settings — not the main password. |
| `BSKY_PDS`          | no       | PDS base URL (default: `https://bsky.social`).                    |

## Usage

### Post text only

```bash
uv run python skills/bluesky/scripts/bluesky.py post --text "Hello from Pillbug"
```

### Post with images

Up to four images per post (ATProto limit). Each image is uploaded as a blob then embedded.

```bash
uv run python skills/bluesky/scripts/bluesky.py post \
  --text "New build running" \
  --image ./shot1.png --image ./shot2.jpg \
  --alt "Terminal output" --alt "Dashboard view"
```

`--alt` values align positionally with `--image`. Missing alts default to an empty string.

On success the script prints the created record `uri` and `cid` as JSON to stdout.

## Notes

- Image MIME type is inferred from the file extension via `mimetypes`.
- Errors from Bluesky are surfaced verbatim to stderr with a non-zero exit code.
