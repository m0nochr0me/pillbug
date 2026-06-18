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

The client sends and receives `message` events. It MAY additionally send an
**audio** payload on the same `message` event (§4 "Inbound audio"), the server
MAY emit `stream` events when the operator has enabled response streaming (§4
"Outbound streaming"), and the server emits an `error` event when it rejects an
input (§4 "Server errors"). A client that handles only text `message` events is
fully conformant; `stream` and `error` matter only for streaming and audio
respectively.

### Inbound (client → server)

Either of these payload shapes is accepted for text:

```js
sock.emit("message", "free-form text");
sock.emit("message", { text: "free-form text" });
```

Empty / whitespace-only text is dropped. Shapes other than the text and audio
forms documented here are silently dropped. The message is forwarded into the
standard Pillbug processing pipeline (security filtering, debounce, agent
invocation).

### Inbound audio (optional)

A client MAY send a voice clip or other audio in the **same `message` event** by
including an `audio` object:

```js
sock.emit("message", {
  audio: {
    data: "<base64-encoded audio bytes>", // required
    mime_type: "audio/webm",              // required, must be audio/*
    filename: "clip.webm"                 // optional
  },
  text: "optional caption or instruction" // optional, accompanies the audio
});
```

Rules the client MUST follow:

- `audio.data` is the audio file's raw bytes, base64-encoded with the standard
  alphabet (not URL-safe). `audio.mime_type` is required and MUST start with
  `audio/` (e.g. `audio/webm`, `audio/ogg`, `audio/mpeg`, `audio/wav`,
  `audio/m4a`).
- `audio.filename` is optional and used only for display and to pick a file
  extension; path components are ignored (only the base name is kept).
- The optional top-level `text` is a caption/instruction that accompanies the
  audio (e.g. "transcribe this", "summarize the voicemail"). Unlike a text-only
  message, an audio message does **not** require non-empty `text`.
- Keep clips small. The server enforces a size cap
  (`PB_WEBSOCKET_MAX_AUDIO_BYTES`, default 8 MB) on the **decoded** bytes;
  base64 inflates the wire payload by ~33%, so budget accordingly.

The server stores the audio in the runtime workspace and forwards it to the
model as a multimodal attachment. The reply arrives as a normal `message` event
(or `stream` deltas followed by a terminal `message`, if streaming is enabled);
there is no separate "audio received" acknowledgement.

**Backend requirement.** Audio understanding requires the runtime to be pointed
at the **real Gemini API** with an audio-capable model. If the operator has
configured a proxy backend (`PB_GEMINI_BASE_URL`), audio is **rejected** with an
`error` event while text messages keep working. Whenever audio is rejected, no
`message` reply follows — the input never enters the agent pipeline.

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

### Outbound streaming (optional)

When the operator has enabled streaming for the websocket channel
(`PB_STREAMING_CHANNELS` includes `websocket`), the server streams the agent's
reply incrementally as `stream` events while the model generates it, then closes
the turn with the normal `message` event carrying the full text:

```json
{
  "session_id": "01HXYZP6Z9N4Y8Q7K3GJ4M5VQ2",
  "delta": "next fragment of the reply"
}
```

Contract the client MUST follow if it consumes `stream` events:

- Each `stream` event carries one `delta` — the next fragment of reply text.
  Concatenate deltas in arrival order to render the reply as it is produced.
- A successfully streamed reply ALWAYS ends with a `message` event whose `text`
  is the complete reply. On that event, **replace** your accumulated delta
  buffer with `message.text` (it equals the concatenated deltas and is
  authoritative). Do not append it to the deltas, or you will render the reply
  twice.
- If a turn fails partway through, the partial `stream` deltas are NOT followed
  by a matching terminal `message`; the runtime's error reply instead arrives as
  a separate, ordinary `message`. A `message` is always the authoritative end of
  a reply — when its `text` does not extend your pending deltas, treat it as a
  fresh message and discard the partial deltas.
- Streaming is opt-in and decided by the operator, not negotiated per
  connection. A client MUST NOT assume `stream` events will arrive. With
  streaming off, the same final text arrives in a single `message` event and no
  `stream` events are sent.
- Tool-use phases are not streamed: deltas begin only once the model starts its
  user-facing answer, so expect a pause — and on tool-only turns, possibly no
  `stream` events at all — before the terminal `message`.

### Server errors (`error` event)

When the server rejects an input it cannot process, it emits an `error` event to
the originating socket instead of a `message`:

```json
{
  "session_id": "01HXYZP6Z9N4Y8Q7K3GJ4M5VQ2",
  "error": "human-readable reason"
}
```

`error` is currently emitted only for rejected **audio** input. Causes include:

- `mime_type` is missing or not `audio/*`.
- A proxy backend (`PB_GEMINI_BASE_URL`) is configured — audio requires the real
  Gemini backend.
- `audio.data` is missing, not valid base64, or decodes to empty.
- The decoded audio exceeds `PB_WEBSOCKET_MAX_AUDIO_BYTES`.

An `error` is terminal for that input: the message is not enqueued and no
`message` reply will follow. Surface it to the user and do not auto-retry the
same payload unless the cause is transient (e.g. shrink an oversized clip and
resend). Plain text input is never rejected this way.

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
  |        emit("stream", {...})  (if enabled)  |
  |<--------------------------------------------|  (zero or more deltas)
  |              emit("message", {...})         |
  |<--------------------------------------------|  (authoritative full text)
  |                                             |
  |   ... (idle longer than IDLE_TIMEOUT) ...   |
  |              forced disconnect              |
  |<--------------------------------------------|
```

Activity that resets the idle timer:

- Any inbound `message` from the client.
- Any outbound `message` emitted by the server (e.g., agent reply, proactive
  message from another channel routed via `send_message`).
- Any outbound `stream` delta emitted during a streamed reply.

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
| `stream` deltas arrive but no terminal `message` follows | The turn failed mid-stream | Discard the partial deltas; the next `message` (an error notice or the next reply) is authoritative — do not render the partial buffer as a finished reply |
| `error` event after sending audio | Audio rejected: non-audio MIME, oversized, invalid base64, or the runtime is on a proxy backend that can't do audio | Read `payload.error`; fix the payload (smaller clip, valid `audio/*`) or fall back to text. Do not retry unchanged |
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

// Progressive rendering: accumulate stream deltas, then let the terminal
// `message` replace the buffer. Works whether or not streaming is enabled —
// with streaming off, only the `message` handler fires.
let pending = "";
sock.on("stream", (payload: { session_id: string; delta: string }) => {
  pending += payload.delta;
  render(pending); // your incremental UI update
});
sock.on("message", (payload: { session_id: string; text: string }) => {
  pending = "";            // terminal message is authoritative
  console.log("agent>", payload.text);
});
// Only needed if you send audio: rejected input arrives as `error`, never `message`.
sock.on("error", (payload: { session_id: string; error: string }) => {
  console.error("rejected:", payload.error); // e.g. clip too large, or proxy backend
});

sock.emit("message", { text: "hello pillbug" });

// Sending audio (e.g. a recorded clip already base64-encoded):
// sock.emit("message", {
//   audio: { data: base64Audio, mime_type: "audio/webm", filename: "clip.webm" },
//   text: "transcribe this",
// });
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

    # Progressive rendering: accumulate stream deltas (only sent when the
    # operator has streaming enabled), then let the terminal `message` replace
    # the buffer. A client that omits this handler still gets the full reply.
    pending = ""

    @sio.on("stream")
    async def on_stream(payload: dict) -> None:
        nonlocal pending
        pending += payload["delta"]
        print(pending, end="\r")  # your incremental UI update

    @sio.on("message")
    async def on_message(payload: dict) -> None:
        nonlocal pending
        pending = ""  # terminal message is authoritative
        print("agent>", payload["text"])

    # Only needed if you send audio: rejected input arrives as `error`.
    @sio.on("error")
    async def on_error(payload: dict) -> None:
        print("rejected:", payload["error"])  # e.g. clip too large, or proxy backend

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
    # Sending audio: base64-encode the bytes and attach an `audio` object.
    # import base64
    # await sio.emit("message", {
    #     "audio": {
    #         "data": base64.b64encode(audio_bytes).decode(),
    #         "mime_type": "audio/webm",
    #         "filename": "clip.webm",
    #     },
    #     "text": "transcribe this",
    # })
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
| `PB_WEBSOCKET_MAX_AUDIO_BYTES` | Max decoded size of an inbound audio clip (default 8 MB); larger clips get an `error` event |
| `PB_GEMINI_BASE_URL` | If set, the runtime uses a proxy backend and **audio input is rejected**; audio requires the real Gemini backend (this var unset) |
| `PB_STREAMING_CHANNELS` | Whether replies stream as `stream` deltas; include `websocket` to enable, omit for `message`-only |

## 10. Stable Contract Summary

A conforming client:

1. Speaks Socket.IO v4 to the configured endpoint and path.
2. Sends `Authorization: Bearer <token>` and `X-SessionID: <ULID>` on every
   connect.
3. Sends user input as `emit("message", "..." or { text: "..." })`, and MAY send
   audio as `emit("message", { audio: { data, mime_type, filename? }, text? })`.
4. Receives agent replies as `on("message", payload => payload.text)`, MAY
   additionally consume `on("stream", payload => payload.delta)` for progressive
   rendering (treating the terminal `message` as authoritative), and — if it
   sends audio — handles `on("error", payload => payload.error)` for rejected
   input.
5. Treats disconnects as recoverable; reuses the ULID to continue, generates a
   new ULID to start a new conversation.
6. Does not log or persist the bearer token client-side beyond what is
   required to maintain the connection.

Anything beyond this contract is implementation-defined and may change.
