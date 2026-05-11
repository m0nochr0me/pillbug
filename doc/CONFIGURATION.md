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

- `PB_RUNTIME_ID`: Stable runtime identifier. When omitted, Pillbug persists one to `~/.pillbug/runtime_id.txt`.
- `PB_AGENT_NAME`: Operator-facing label shown in telemetry and published metadata.
- `PB_WORKSPACE_ROOT`: Runtime workspace root. Defaults to the workspace created under the configured base directory.
- `PB_SECURITY_PATTERNS_PATH`: Path to the user-editable warning and block regex file.
- `PB_INBOUND_DEBOUNCE_SECONDS`: Per-session debounce window used before batching inbound messages.

## Model Backend

Pillbug talks to Gemini through the `google-genai` SDK and selects a backend with `PB_GEMINI_BACKEND`.

- `PB_GEMINI_BACKEND`: `developer` (default) or `vertex`.
- `PB_GEMINI_MODEL`: Model id used for chat sessions.
- `PB_GEMINI_TEMPERATURE`, `PB_GEMINI_TOP_P`, `PB_GEMINI_MAX_OUTPUT_TOKENS`, `PB_GEMINI_THINKING_LEVEL`: Standard sampling and reasoning controls.
- `PB_GEMINI_RESPONSE_TIMEOUT_SECONDS`: Timeout applied to each model response.
- `PB_GEMINI_MAX_AFC_CALLS`: Hard cap on automatic function-calling iterations per turn.
- `PB_GEMINI_EMPTY_RESPONSE_MAX_NUDGES`: How many times the runtime retries an empty model response before giving up.

### Developer backend (Google AI Studio / proxy)

- `PB_GEMINI_API_KEY`: Required when `PB_GEMINI_BACKEND=developer`.
- `PB_GEMINI_BASE_URL`: Optional override that redirects the SDK at a different `generateContent` endpoint. Only honored in `developer` mode. Use it to point Pillbug at the `pillbug-genai-proxy` translator so an OpenAI-compatible upstream (llama.cpp, vLLM, LiteLLM, Ollama) handles inference while the runtime keeps using the Gemini wire format.

OpenAI-compatible local model example:

```bash
PB_GEMINI_BACKEND=developer
PB_GEMINI_API_KEY=dummy
PB_GEMINI_BASE_URL=http://127.0.0.1:9000
PB_GEMINI_MODEL=gemma-3-12b
```

The proxy itself is a separate service configured through `PB_GENAI_PROXY_*` variables — see [packages/pillbug-genai-proxy/README.md](../packages/pillbug-genai-proxy/README.md).

### Vertex backend

- `PB_GEMINI_VERTEX_PROJECT` and `PB_GEMINI_VERTEX_LOCATION`: Required when `PB_GEMINI_BACKEND=vertex`.
- `PB_GEMINI_VERTEX_CREDENTIALS_PATH`: Optional service-account JSON file. When omitted, application default credentials are used.
- `PB_GEMINI_BASE_URL` is rejected in vertex mode.

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

## Optional Matrix Settings

Enabled with `PB_ENABLED_CHANNELS=...,matrix` and `PB_CHANNEL_PLUGIN_FACTORIES=matrix=pillbug_matrix.matrix_channel:create_channel`. Acquire the access token once with `uv run pillbug-matrix-access-token`.

- `PB_MATRIX_HOMESERVER_URL`: Required. Matrix homeserver URL.
- `PB_MATRIX_USER_ID`: Required. Full Matrix user ID such as `@pillbug:example.org`.
- `PB_MATRIX_ACCESS_TOKEN`: Required. Pre-obtained access token used by the runtime client.
- `PB_MATRIX_DEVICE_ID`: Optional device ID to reuse for the runtime client.
- `PB_MATRIX_ALLOWED_ROOM_IDS`: Optional CSV allowlist of room IDs. When set, events from other rooms are dropped.
- `PB_MATRIX_SYNC_TIMEOUT_MS`: `/sync` long-poll timeout in milliseconds (default `30000`).
- `PB_MATRIX_REPLY_TO_MESSAGE`: Whether replies include `m.in_reply_to` metadata (default `true`).

Matrix inbound attachments are downloaded into `downloads/matrix/<sanitized-room-id>/` inside the workspace and forwarded through generic `inbound_attachments` metadata. End-to-end encryption is not enabled in the current build.

## Optional WebSocket Settings

Enabled with `PB_ENABLED_CHANNELS=...,websocket` and `PB_CHANNEL_PLUGIN_FACTORIES=websocket=pillbug_websocket:create_channel`. Clients connect with `Authorization: Bearer <token>` plus an `X-SessionID` ULID that becomes the Pillbug `conversation_id`.

- `PB_WEBSOCKET_BEARER_TOKEN`: Required. Shared secret validated on Socket.IO handshake.
- `PB_WEBSOCKET_HOST`: Bind address (default `127.0.0.1`).
- `PB_WEBSOCKET_PORT`: Listen port (default `9200`).
- `PB_WEBSOCKET_IDLE_TIMEOUT_SECONDS`: Disconnect sessions idle for this long (default `600.0`).
- `PB_WEBSOCKET_JANITOR_INTERVAL_SECONDS`: Idle-session sweeper interval (default `30.0`).
- `PB_WEBSOCKET_CORS_ALLOWED_ORIGINS`: `*` or CSV of allowed origins (default `*`).
- `PB_WEBSOCKET_SOCKETIO_PATH`: Socket.IO endpoint path (default `/socket.io`).

## Optional Trigger Settings

Enabled with `PB_ENABLED_CHANNELS=...,trigger` and `PB_CHANNEL_PLUGIN_FACTORIES=trigger=pillbug_trigger:create_channel`. Per-source reaction prompts live in `trigger_sources.json`; see [packages/pillbug-trigger/README.md](../packages/pillbug-trigger/README.md).

- `PB_TRIGGER_BEARER_TOKEN`: Required. Bearer token validated on `POST /trigger`.
- `PB_TRIGGER_HOST`: Bind address (default `127.0.0.1`).
- `PB_TRIGGER_PORT`: Listen port (default `9100`).
- `PB_TRIGGER_SOURCES_PATH`: Path to the source config file (default `~/.pillbug/trigger_sources.json`).

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
