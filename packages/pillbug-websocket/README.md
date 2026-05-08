# pillbug-websocket

Socket.IO websocket channel plugin for Pillbug. Each client connection presents a
client-generated [ULID](https://github.com/ulid/spec) as `X-SessionID` and that ULID
becomes the Pillbug `conversation_id` for the lifetime of the session.

## Installation

```bash
uv sync --extra websocket
```

## Configuration

| Variable | Required | Description |
| - | - | - |
| `PB_WEBSOCKET_BEARER_TOKEN` | Yes | Pre-shared API key clients send as `Authorization: Bearer <key>` |
| `PB_WEBSOCKET_HOST` | No | Bind address (default `127.0.0.1`) |
| `PB_WEBSOCKET_PORT` | No | Listen port (default `9200`) |
| `PB_WEBSOCKET_IDLE_TIMEOUT_SECONDS` | No | Disconnect sessions idle for this long (default `600.0`) |
| `PB_WEBSOCKET_JANITOR_INTERVAL_SECONDS` | No | How often the idle-session sweeper runs (default `30.0`) |
| `PB_WEBSOCKET_CORS_ALLOWED_ORIGINS` | No | `*` or a comma-separated list of allowed origins (default `*`) |
| `PB_WEBSOCKET_SOCKETIO_PATH` | No | Socket.IO endpoint path (default `/socket.io`) |

Enable the channel:

```bash
export PB_ENABLED_CHANNELS=cli,websocket
export PB_CHANNEL_PLUGIN_FACTORIES=websocket=pillbug_websocket:create_channel
export PB_WEBSOCKET_BEARER_TOKEN=your-secret-key
```

## Connection Contract

Each new connection is a separate session. The client owns the session ID:

- `Authorization: Bearer <PB_WEBSOCKET_BEARER_TOKEN>` — required, validated on connect
- `X-SessionID: <ULID>` — required, must match the 26-char Crockford-Base32 ULID alphabet

The channel uses the ULID as `conversation_id`, so the runtime keys its
`GeminiChatSession` (and Pillbug session memory) by that ID. A client that
reconnects with the same ULID before the idle timeout continues the same chat;
a fresh ULID always starts a new session.

A connection that fails to provide a valid bearer token or a valid ULID is
refused on the Socket.IO handshake.

## Inbound and Outbound Events

| Event | Direction | Payload |
| - | - | - |
| `message` | client → server | string, or `{ "text": "..." }` |
| `message` | server → client | `{ "session_id": "<ULID>", "text": "..." }` |

Empty payloads are ignored. Outbound messages are emitted to every socket
currently registered for the session ID; in normal operation that is exactly
one socket.

## Idle Timeout

Activity is tracked per session ID (any inbound `message` or any outbound
emit resets the timer). A background task scans sessions every
`PB_WEBSOCKET_JANITOR_INTERVAL_SECONDS` and disconnects sockets whose
session has been idle for longer than `PB_WEBSOCKET_IDLE_TIMEOUT_SECONDS`.

## Example Client

```js
import { io } from "socket.io-client";

const sock = io("http://127.0.0.1:9200", {
  path: "/socket.io",
  transports: ["websocket"],
  extraHeaders: {
    Authorization: "Bearer your-secret-key",
    "X-SessionID": "01HXYZP6Z9N4Y8Q7K3GJ4M5VQ2",
  },
});

sock.on("message", (payload) => console.log("agent:", payload.text));
sock.emit("message", { text: "hello pillbug" });
```

> Browser Socket.IO clients cannot set arbitrary request headers when using
> the WebSocket transport; in that environment use the `polling` transport for
> the handshake or a thin proxy that injects the headers.
