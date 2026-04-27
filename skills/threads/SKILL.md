---
name: threads
description: Publish posts to Threads (Meta). Use when the agent needs to create a Threads post.
---

# Threads

Publish posts to a Threads (Meta) account from the CLI. The helper is a
single-file Python script that talks to the Threads Graph API directly.

Script: `scripts/threads.py`

## Environment

Set the app-level credentials in the environment (or a `.env` file in the
skill directory or current working directory). User-level OAuth tokens are
obtained via `setup` and stored separately on disk — never put a user
access token in the environment.

| Variable                | Required | Purpose                                                                |
| ----------------------- | -------- | ---------------------------------------------------------------------- |
| `THREADS_APP_ID`        | yes      | Meta app id (numeric).                                                 |
| `THREADS_APP_SECRET`    | yes      | Meta app secret.                                                       |
| `THREADS_REDIRECT_URI`  | setup    | OAuth callback URL registered in the Meta app dashboard (HTTPS).       |
| `THREADS_SCOPES`        | no       | Override scopes (default: `threads_basic,threads_content_publish`).    |

## One-time setup

The Threads Graph API uses an OAuth 2.0 authorization-code flow. The
`setup` command walks through it and persists the resulting long-lived
user access token (~60-day validity) to `skills/threads/.credentials.json`.

```bash
uv run python skills/threads/scripts/threads.py setup
```

The script prints an authorization URL. Open it in a browser, approve the
app, and Meta will redirect to `THREADS_REDIRECT_URI` with `?code=...`
appended. Paste either the entire redirected URL or just the `code` value
back into the prompt.

For non-interactive use, pass the code directly:

```bash
uv run python skills/threads/scripts/threads.py setup \
  --redirect-uri "https://example.com/threads/callback" \
  --code "AQB..."
```

After success, `.credentials.json` contains `user_id`, `username`,
`access_token`, and `expires_at`. The file is created with `0600`
permissions and is excluded from git via the root `.gitignore`.

### Refreshing the token

Long-lived tokens can be refreshed once per token (extends expiry by
~60 days). Re-run `setup` if the token has already expired.

```bash
uv run python skills/threads/scripts/threads.py refresh
```

## Posting

### Text only

```bash
uv run python skills/threads/scripts/threads.py post --text "Hello from Pillbug"
```

### With images

The Threads Graph API does **not** accept binary uploads — images must
already be hosted at a publicly reachable URL. Pass the URL with
`--image-url`. Up to 20 items per carousel.

```bash
uv run python skills/threads/scripts/threads.py post \
  --text "New build running" \
  --image-url "https://example.com/shot1.png" \
  --image-url "https://example.com/shot2.jpg" \
  --alt "Terminal output" \
  --alt "Dashboard view"
```

`--alt` values align positionally with `--image-url`. Missing alts are
omitted from the request.

On success the script prints the published thread `id` as JSON to stdout.

## Notes

- A single image post creates one container, waits for it to reach
  `FINISHED` state, then publishes.
- Multiple images create one child container per image plus a `CAROUSEL`
  parent; all containers are awaited before publishing.
- Container readiness polling defaults to a 60-second timeout with 2-second
  intervals. Hosted images that take longer to fetch will surface the last
  observed status in the error message.
- Errors from the Threads Graph API are surfaced verbatim to stderr with a
  non-zero exit code.
