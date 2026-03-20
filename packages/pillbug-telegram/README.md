# pillbug-telegram

Telegram channel plugin for Pillbug, implemented as a uv workspace member.

Enable it from the root application with:

```bash
uv sync --extra telegram
export PB_ENABLED_CHANNELS=cli,telegram
export PB_CHANNEL_PLUGIN_FACTORIES=telegram=pillbug_telegram.telegram_channel:create_channel
export PB_TELEGRAM_BOT_TOKEN=your_bot_token
export PB_TELEGRAM_ALLOWED_CHAT_IDS=123456789,-100987654321
uv run python -m app
```

If `PB_TELEGRAM_ALLOWED_CHAT_IDS` is set, the plugin only accepts inbound updates from those Telegram chat IDs.
Messages from other chat IDs are ignored and logged as warnings.

On startup the plugin also refreshes the bot command list so Telegram only shows `/start` and `/clear`.
`/start` is handled locally by the plugin and replies with `ok` without invoking the LLM.

Inbound Telegram photos, videos, documents, audio files, and voice messages are downloaded into the Pillbug workspace under `downloads/telegram/<chat_id>/`.
The resulting workspace-relative path is included in the inbound message text and metadata so the runtime can reference the saved file.

When a downloaded attachment is a supported Gemini multimodal input, Pillbug also uploads it with the chat request.
Current multimodal forwarding is limited to PDFs, audio files, images, and plaintext or Markdown files.
Other attachment types, such as video, are still saved into the workspace and referenced in text only.

The forwarding path is now channel-agnostic: any inbound message metadata that includes `inbound_attachments` entries with workspace paths can be consumed by the Gemini layer.
That means future channels, local workspace file pickers, or URL-download flows can reuse the same multimodal path without depending on Telegram-specific metadata keys.
