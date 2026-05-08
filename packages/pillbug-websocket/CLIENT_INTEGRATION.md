# Pillbug Websocket Client Integration Guide

This guide is the source of truth for any client (web app, native app, or
another coding agent) that needs to connect to a Pillbug runtime through the
`pillbug-websocket` channel. Implement against this document — do not infer
behavior from the runtime source.

## 1. Transport

- Protocol: [Socket.IO v4](https://socket.io/docs/v4/) (NOT plain WebSocket).
  The server is built on `python-socketio.AsyncServer`, so a raw `ws://` client
  will fail the handshake.
- Default endpoint: `http://<PB_WEBSOCKET_HOST>:<PB_WEBSOCKET_PORT>` (defaults
  `127.0.0.1:9200`).
- Default Socket.IO path: `/socket.io` (override with
  `PB_WEBSOCKET_SOCKETIO_PATH`).
- TLS is the operator's responsibility (typically a reverse proxy in front of
  the channel). Use `https://` / `wss://` when the proxy terminates TLS.
- Both `polling` and `websocket` transports are accepted. `websocket` is
  preferred where headers can be set on the upgrade.

## 2. Authentication

Every connection MUST present a pre-shared API key:

```
Authorization: Bearer <PB_WEBSOCKET_BEARER_TOKEN>
```

The server compares the value byte-for-byte against `PB_WEBSOCKET_BEARER_TOKEN`
on the connect handshake. Missing or mismatched tokens cause the handshake to
fail with a Socket.IO `connect_error` event (HTTP 400 in some clients) and the
socket is never opened.

The token MUST be sent as a request header. Other transports (query string,
`auth` payload, cookies) are intentionally not honored.

## 3. Session Identity (`X-SessionID`)

Every connection MUST also send:

```
X-SessionID: <ULID>
```

Rules the client MUST follow:

- Generate the ID using a [ULID](https://github.com/ulid/spec) implementation
  (Crockford Base32, 26 characters, alphabet `0123456789ABCDEFGHJKMNPQRSTVWXYZ`).
- Send it in upper case. The server normalizes to upper case but rejects
  anything that is not exactly 26 valid characters.
- The ULID becomes the Pillbug `conversation_id`. The runtime keys its chat
  session, debounce buffer, and stored history on `websocket:<ULID>`. Two
  consequences:
  - Reusing the same ULID across reconnects continues the same agent
    conversation (subject to the idle timeout — see §6).
  - Generating a fresh ULID always starts a brand new conversation with no
    prior history.
- Treat the ULID as session-private. Do not log it alongside the user's PII or
  share it across users.

A connection with a missing, malformed, or non-ULID `X-SessionID` is refused
on the handshake.

### Browser caveat

Browser Socket.IO clients (`socket.io-client` in a browser) cannot set
arbitrary headers when the transport is `websocket`. Two viable options:

1. Force `transports: ["polling"]` (or allow polling first, then upgrade);
   custom headers via `extraHeaders` are honored on the polling handshake.
2. Put a thin proxy (Nginx, a service worker, an Electron preload, a tunneling
   server) in front of the channel that injects the headers.

Native and server-side clients (Node, Python, Go, Rust) can always set headers
on the websocket upgrade directly.

## 4. Message Protocol

Only one event name is used in both directions: `message`.

### Inbound (client → server)

Either of these payload shapes is accepted:

```js
sock.emit("message", "free-form text");
sock.emit("message", { text: "free-form text" });
```

Other shapes are silently dropped. Empty / whitespace-only text is dropped.
The message is forwarded into the standard Pillbug processing pipeline
(security filtering, debounce, agent invocation).

### Outbound (server → client)

The server emits `message` events of this shape:

```json
{
  "session_id": "01HXYZP6Z9N4Y8Q7K3GJ4M5VQ2",
  "text": "agent reply text"
}
```

`session_id` always equals the ULID the client sent on connect. `text` is the
agent's user-facing reply. Treat any other fields as reserved for future use
and ignore them.

The server emits to every socket currently registered for that session ID. In
normal operation that is exactly one socket; if the operator has connected
twice with the same ULID they will both receive the reply.

## 5. Connection Lifecycle

```
[client]                                     [pillbug-websocket]
  |  HTTP upgrade w/ Authorization+X-SessionID  |
  |-------------------------------------------->|
  |                                             |  validate bearer
  |                                             |  validate ULID
  |              connect ack / refusal          |  register session
  |<--------------------------------------------|
  |                                             |
  |   emit("message", "...")                    |
  |-------------------------------------------->|
  |                                             |  -> InboundMessage
  |                                             |  -> agent pipeline
  |                                             |
  |              emit("message", {...})         |
  |<--------------------------------------------|
  |                                             |
  |   ... (idle longer than IDLE_TIMEOUT) ...   |
  |              forced disconnect              |
  |<--------------------------------------------|
```

Activity that resets the idle timer:

- Any inbound `message` from the client.
- Any outbound `message` emitted by the server (e.g., agent reply, proactive
  message from another channel routed via `send_message`).

Socket.IO heartbeats / pings do **not** count as activity.

## 6. Idle Timeout and Reconnection

- A background sweeper runs every `PB_WEBSOCKET_JANITOR_INTERVAL_SECONDS`
  (default 30s).
- Sessions whose last activity is older than
  `PB_WEBSOCKET_IDLE_TIMEOUT_SECONDS` (default 600s) are evicted; their
  sockets are forcibly disconnected.
- After eviction the channel-level mapping is gone; the runtime's chat
  history for that ULID is retained until the runtime itself decides to
  summarize it (see `PB_SESSION_SUMMARIZATION`).

Recommended client behavior:

- If the user goes idle but you want continuity, send a periodic no-op message
  (e.g., a documented `ping` payload your agent ignores) shorter than the
  configured idle timeout. The default 600s leaves comfortable room for a 5
  minute heartbeat.
- On unexpected disconnect, reconnect with the **same** ULID and the
  conversation continues.
- If the user has explicitly ended the session, generate a **new** ULID before
  reconnecting. Do not reuse a ULID across distinct user sessions.
- Do not retry a connection that was refused with `connect_error` due to
  invalid auth or invalid ULID — the cause will not change without action from
  the user.

## 7. Error Handling

| Symptom | Likely cause | Client action |
| - | - | - |
| `connect_error` immediately after connect | Wrong bearer token, missing/invalid `X-SessionID`, server not running | Surface to the user; do not auto-retry without user input |
| Connection silently drops, can reconnect | Idle timeout or operator restart | Reconnect with same ULID for continuity, or new ULID for a fresh session |
| Outbound `message` never arrives after sending one | Agent is still processing (no ack model is provided), or the runtime debounced multiple messages into a single batch | Show a pending indicator; do not retransmit (the runtime de-duplicates by message id only within a debounce window) |
| Repeated `connect_error` with valid creds | Server-side bearer changed, or the runtime is shutting down | Surface to the user; back off |

There is no application-level acknowledgement. If you need delivery guarantees
beyond TCP+Socket.IO, layer them on top via your own message ids in the `text`
payload and have the agent echo them back.

## 8. Reference Clients

### JavaScript / TypeScript (Node)

```ts
import { io } from "socket.io-client";
import { ulid } from "ulid";

const sessionId = ulid();
const sock = io("http://127.0.0.1:9200", {
  path: "/socket.io",
  transports: ["websocket"],
  reconnection: true,
  extraHeaders: {
    Authorization: `Bearer ${process.env.PILLBUG_TOKEN!}`,
    "X-SessionID": sessionId,
  },
});

sock.on("connect", () => console.log("connected", sessionId));
sock.on("connect_error", (err) => console.error("refused", err.message));
sock.on("disconnect", (reason) => console.log("disconnected:", reason));
sock.on("message", (payload: { session_id: string; text: string }) => {
  console.log("agent>", payload.text);
});

sock.emit("message", { text: "hello pillbug" });
```

### Python

```python
import os
import asyncio
import socketio
from ulid import ULID  # python-ulid

async def main() -> None:
    session_id = str(ULID())
    sio = socketio.AsyncClient()

    @sio.event
    async def connect() -> None:
        print("connected", session_id)

    @sio.event
    async def connect_error(data: object) -> None:
        print("refused", data)

    @sio.on("message")
    async def on_message(payload: dict) -> None:
        print("agent>", payload["text"])

    await sio.connect(
        "http://127.0.0.1:9200",
        socketio_path="/socket.io",
        transports=["websocket"],
        headers={
            "Authorization": f"Bearer {os.environ['PILLBUG_TOKEN']}",
            "X-SessionID": session_id,
        },
    )
    await sio.emit("message", {"text": "hello pillbug"})
    await sio.wait()

asyncio.run(main())
```

### Browser (with polling fallback)

```js
import { io } from "socket.io-client";
import { ulid } from "ulid";

const sock = io("https://pillbug.example.com", {
  path: "/socket.io",
  transports: ["polling", "websocket"],
  upgrade: true,
  extraHeaders: {
    Authorization: `Bearer ${TOKEN}`,
    "X-SessionID": ulid(),
  },
});
```

(`extraHeaders` is honored only on the polling handshake in browsers; the
websocket upgrade then reuses the established Socket.IO session.)

## 9. Operator Configuration the Client Should Know About

The client cannot read these directly, but should know they exist when
diagnosing integration issues:

| Variable | Effect on the client |
| - | - |
| `PB_WEBSOCKET_HOST` / `PB_WEBSOCKET_PORT` | Connection URL |
| `PB_WEBSOCKET_SOCKETIO_PATH` | `path` option in the Socket.IO client |
| `PB_WEBSOCKET_BEARER_TOKEN` | Value to send in `Authorization` |
| `PB_WEBSOCKET_IDLE_TIMEOUT_SECONDS` | How long without activity before forced disconnect |
| `PB_WEBSOCKET_CORS_ALLOWED_ORIGINS` | Browser origins the server accepts; `*` allows all |

## 10. Stable Contract Summary

A conforming client:

1. Speaks Socket.IO v4 to the configured endpoint and path.
2. Sends `Authorization: Bearer <token>` and `X-SessionID: <ULID>` on every
   connect.
3. Sends user input as `emit("message", "..." or { text: "..." })`.
4. Receives agent replies as `on("message", payload => payload.text)`.
5. Treats disconnects as recoverable; reuses the ULID to continue, generates a
   new ULID to start a new conversation.
6. Does not log or persist the bearer token client-side beyond what is
   required to maintain the connection.

Anything beyond this contract is implementation-defined and may change.
