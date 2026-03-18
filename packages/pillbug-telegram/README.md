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
