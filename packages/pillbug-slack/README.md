# pillbug-slack

Slack channel plugin for Pillbug, implemented as a uv workspace member. Uses Slack [Socket Mode](https://api.slack.com/apis/socket-mode) so the runtime does not need a publicly reachable HTTP endpoint.

## Slack app setup

Create a Slack app with the following:

- **Socket Mode** enabled.
- **App-level token** (`xapp-...`) with the `connections:write` scope. This is `PB_SLACK_APP_TOKEN`.
- **Bot user** with these scopes (Bot Token Scopes):
  - `app_mentions:read`
  - `chat:write`
  - `im:history`
  - `im:read`
  - `channels:history` (only if the bot should respond in public channel threads)
  - `groups:history` (only if the bot should respond in private channel threads)
  - `files:read` (only if inbound file forwarding is desired)
  - `files:write` (only if outbound attachments are desired)
- Subscribed events (Event Subscriptions → Subscribe to bot events):
  - `app_mention`
  - `message.im`
  - `message.channels` (only if responding in public-channel threads)
  - `message.groups` (only if responding in private-channel threads)
- Install the app to the workspace to obtain the bot token (`xoxb-...`). This is `PB_SLACK_BOT_TOKEN`.

## Enable in Pillbug

```bash
uv sync --extra slack
export PB_ENABLED_CHANNELS=cli,slack
export PB_CHANNEL_PLUGIN_FACTORIES=slack=pillbug_slack.slack_channel:create_channel
export PB_SLACK_APP_TOKEN=xapp-...
export PB_SLACK_BOT_TOKEN=xoxb-...
# Optional: restrict to specific Slack channel IDs (comma-separated).
export PB_SLACK_ALLOWED_CHANNEL_IDS=C0123456789,D0123456789
uv run python -m app
```

## Behavior

- **Direct messages** are treated as a single conversation per DM channel. `conversation_id` is the Slack channel ID (e.g. `D012345`).
- **Channel threads** (after an `app_mention` or a reply in a thread the bot was tagged in) become their own session. `conversation_id` is `<channel_id>:<thread_ts>`, so each thread keeps its own LLM history.
- The plugin ignores top-level messages in public/private channels — the bot only acts on direct messages, explicit `@`-mentions, and replies in threads it is already part of.
- Bot-authored messages (`bot_id` present or matching `user_id`) and common housekeeping subtypes (`message_changed`, joins, leaves, topic changes) are filtered out.
- Outbound responses always reply in the originating thread when `PB_SLACK_REPLY_IN_THREAD=true` (default). Set it to `false` to post to the channel root instead.
- Long responses are split into ~3500-character chunks so Slack accepts each one comfortably.

## File attachments

- Inbound files attached to messages are downloaded with the bot token from `url_private_download` and saved under `downloads/slack/<channel_id>/`.
- The workspace-relative path is referenced in the inbound message text and stored under `inbound_attachments` metadata, so the generic Gemini multimodal forwarding path in `app/core/ai.py` picks them up without any Slack-specific branching.
- Outbound attachments are sent via `files_upload_v2` and respect the originating thread.

## Sending from `send_message`

External invocations can use the local MCP `send_message` tool. Slack accepts:

- `slack:C0123456789` — post to the channel root.
- `slack:C0123456789:1700000000.000100` — post into a specific thread.

The same encoding is used as `conversation_id` for inbound messages, so replies and follow-ups flow through unchanged.

## Limitations

- Slack does not expose a Web API typing indicator for plain channels and DMs, so `response_presence` is a no-op. The runtime debounce window plus the chunked reply path keep responses readable in practice.
- Encrypted/restricted file modes (`hidden_by_limit`, `tombstone`) are skipped.
