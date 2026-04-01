# Configuration Reference

Pillbug reads runtime configuration from `PB_` environment variables through `app/core/config.py`.
For installation and deployment steps, see [Installation Instructions](./INSTALL.md).

For working examples, start from the files under `doc/simple/` and `doc/multi/`:

- `doc/simple/example_runtime.env`
- `doc/multi/example_runtime-a.env`
- `doc/multi/example_runtime-b.env`
- `doc/multi/example_dashboard.env`
- `doc/multi/example_arca.env`

## Core Runtime

- `PB_GEMINI_API_KEY`: Gemini API key required for model access.
- `PB_RUNTIME_ID`: Stable runtime identifier. When omitted, Pillbug persists one to `~/.pillbug/runtime_id.txt`.
- `PB_AGENT_NAME`: Operator-facing label shown in telemetry and published metadata.
- `PB_WORKSPACE_ROOT`: Runtime workspace root. Defaults to the workspace created under the configured base directory.
- `PB_SECURITY_PATTERNS_PATH`: Path to the user-editable warning and block regex file.
- `PB_INBOUND_DEBOUNCE_SECONDS`: Per-session debounce window used before batching inbound messages.

## Channels And Plugins

- `PB_ENABLED_CHANNELS`: Comma-separated enabled channels such as `cli`, `telegram`, or `a2a`.
- `PB_CHANNEL_PLUGIN_FACTORIES`: Plugin factory mapping in `channel=package.module:factory` format.

CLI-only example:

```bash
PB_ENABLED_CHANNELS=cli
PB_CHANNEL_PLUGIN_FACTORIES=
```

Telegram example:

```bash
PB_ENABLED_CHANNELS=cli,telegram
PB_CHANNEL_PLUGIN_FACTORIES=telegram=pillbug_telegram.telegram_channel:create_channel
PB_TELEGRAM_BOT_TOKEN=your_bot_token
```

A2A example:

```bash
PB_ENABLED_CHANNELS=cli,a2a
PB_CHANNEL_PLUGIN_FACTORIES=a2a=pillbug_a2a.a2a_channel:create_channel
PB_A2A_SELF_BASE_URL=http://runtime-a:8000
PB_A2A_BEARER_TOKEN=shared-a2a-bearer-token
PB_A2A_PEERS_JSON='[{"runtime_id":"runtime-b","base_url":"http://runtime-b:8000"}]'
```

## Session Summarization

- `PB_SESSION_SUMMARIZATION`: Enables automatic summarization. Supported values are `memory`, `compress`, or disabled.
- `PB_SESSION_SUMMARIZATION_THRESHOLD`: Total token threshold that triggers automatic summarization.

Mode behavior:

- `memory`: summarize and reset stored Gemini history.
- `compress`: replace earlier Gemini history with a single synthetic summary message.

## A2A And Agent Card

- `PB_A2A_SELF_BASE_URL`: Public base URL peers use for replies.
- `PB_A2A_BEARER_TOKEN`: Bearer token required for `POST /a2a/messages` and authenticated Agent Card access.
- `PB_A2A_OUTBOUND_TIMEOUT_SECONDS`: Timeout for outbound A2A delivery attempts.
- `PB_A2A_PEERS_JSON`: JSON array of known peers and their base URLs.
- `PB_A2A_CONVERGENCE_MAX_HOPS`: Maximum automatic cross-runtime reply depth.
- `PB_A2A_AGENT_DESCRIPTION`: Short description published in the Agent Card.
- `PB_A2A_PROVIDER_ORGANIZATION`: Provider organization name for published A2A metadata.
- `PB_A2A_PROVIDER_URL`: Provider home page URL.
- `PB_A2A_DOCUMENTATION_URL`: Documentation link published in the Agent Card.
- `PB_A2A_ICON_URL`: Icon URL published in the Agent Card.

## Dashboard And Control Access

- `PB_DASHBOARD_BEARER_TOKEN`: Bearer token for telemetry and operator control endpoints.

If both `PB_DASHBOARD_BEARER_TOKEN` and `PB_A2A_BEARER_TOKEN` are configured, they must differ so dashboard access stays isolated from peer-runtime access.

## Scheduler And Task Storage

- `PB_DOCKET_URL`: Optional Redis-backed Docket endpoint for scheduled tasks.
- `PB_DOCKET_NAME`: Base Docket namespace. Pillbug derives a per-runtime namespace from this value and the runtime id.

Scheduled tasks are persisted at `~/.pillbug/tasks/agent_tasks.json` when using local storage.

## URL Fetching Limits

- `PB_MCP_FETCH_URL_MAX_BYTES`: Maximum streamed download size before a fetch is stopped.
- `PB_MCP_FETCH_URL_OUTPUT_DIR`: Workspace-relative directory where fetched resources are saved.
- `PB_MCP_FETCH_URL_TIMEOUT_SECONDS`: Timeout for remote fetch requests.

## Optional Telegram Settings

- `PB_TELEGRAM_BOT_TOKEN`: Telegram bot token.
- `PB_TELEGRAM_ALLOWED_UPDATES`: CSV list such as `message,edited_message`.
- `PB_TELEGRAM_POLL_TIMEOUT_SECONDS`: Long-poll timeout.
- `PB_TELEGRAM_POLL_LIMIT`: Maximum updates requested per poll.
- `PB_TELEGRAM_REPLY_TO_MESSAGE`: Whether replies are threaded to the inbound message.
- `PB_TELEGRAM_DELETE_WEBHOOK_ON_START`: Removes an existing webhook before polling starts.
- `PB_TELEGRAM_DROP_PENDING_UPDATES`: Discards queued Telegram updates on startup.

## Deployment Notes

- The first runtime launch is expected to exit after initializing the workspace and seeding `workspace/AGENTS.md`.
- Copy repository skills into `workspace/skills/` after that bootstrap run if you want bundled skills available at runtime.
- When trigger support is enabled, populate `trigger_sources.json`. If trigger support is disabled, use `[]`.
- When external MCP servers are not needed, `mcp.json` can be omitted or reduced to:

```json
{
  "servers": {},
  "inputs": []
}
```
