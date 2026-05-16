# pillbug-trigger

External event trigger channel plugin for Pillbug. Receives debounced events over HTTP and routes them to the agent runtime with configurable reaction prompts.

## Installation

```bash
uv sync --extra trigger
```

## Configuration

| Variable | Required | Description |
| - | - | - |
| `PB_TRIGGER_BEARER_TOKEN` | Yes | Bearer token for authenticating trigger submissions |
| `PB_TRIGGER_HOST` | No | Bind address (default `127.0.0.1`) |
| `PB_TRIGGER_PORT` | No | Listen port (default `9100`) |
| `PB_TRIGGER_SOURCES_PATH` | No | Path to the trigger source config file (default: `~/.pillbug/trigger_sources.json`) |

Enable the channel:

```bash
export PB_ENABLED_CHANNELS=cli,trigger
export PB_CHANNEL_PLUGIN_FACTORIES=trigger=pillbug_trigger:create_channel
export PB_TRIGGER_BEARER_TOKEN=your-secret-token
```

Optionally register the trigger sources management MCP tool by adding the
plugin to `PB_MCP_TOOL_FACTORIES`:

```bash
export PB_MCP_TOOL_FACTORIES=trigger=pillbug_trigger:register_trigger_tools
```

The plugin self-gates on `trigger` being in `PB_ENABLED_CHANNELS`, so leaving
the entry in place on a runtime that hasn't enabled the channel is harmless.

## Source Configuration

Per-source reaction prompts now live in a JSON file. By default the trigger channel creates `~/.pillbug/trigger_sources.json` on first use.

You can override that location with `PB_TRIGGER_SOURCES_PATH`, but the normal path is:

```bash
~/.pillbug/trigger_sources.json
```

Example file contents:

```bash
cat > ~/.pillbug/trigger_sources.json <<'EOF'
[
  {
    "source": "server-monitor",
    "prompt": "Server alert: {title}. Details: {body}. Run diagnostics and notify the user."
  },
  {
    "source": "weather-alert",
    "prompt": "Public safety alert: {title}. {body}. Notify the user immediately, run backups, and schedule a follow-up checkup.",
    "urgency_override": "high"
  },
  {
    "source": "stock-tracker",
    "prompt": "Stock price change: {title}. {body}. Analyze whether this is worth the user's attention and suggest actions if so.",
    "urgency_override": "low"
  }
]
EOF
```

## Urgency Levels & Debounce

Events carry an urgency level that controls how long the channel waits before batching and forwarding to the agent:

| Urgency | Debounce Window | Use Case |
| - | - | - |
| `low` | 30 seconds | Stock tickers, non-critical metrics |
| `med` | 10 seconds | Server monitoring, moderate alerts |
| `high` | 1 second | Natural disasters, safety alerts |

Source configs can override the urgency from the event payload using `urgency_override`.

## Submitting Events

```bash
curl -X POST http://127.0.0.1:9100/trigger \
  -H "Authorization: Bearer your-secret-token" \
  -H "Content-Type: application/json" \
  -d '{
    "source": "server-monitor",
    "urgency": "med",
    "title": "web-01 health check failed",
    "body": "HTTP 503 on /health for 3 consecutive checks"
  }'
```

### Event Schema

| Field | Type | Required | Description |
| - | - | - | - |
| `source` | string | Yes | Trigger source identifier |
| `urgency` | `low` / `med` / `high` | No | Urgency level (default `med`) |
| `title` | string | Yes | Short event summary |
| `body` | string | No | Detailed description |
| `conversation_id` | string | No | Route to specific conversation (default: source name) |
| `metadata` | object | No | Arbitrary key-value data |

## How It Works

1. External system sends POST to `/trigger` with bearer auth
2. Event is buffered by source + conversation + urgency
3. After the debounce window expires, buffered events are batched into a single `InboundMessage`
4. If a source config exists, the configured prompt template is used; otherwise a default format is applied
5. The message enters the normal Pillbug runtime pipeline for agent processing
