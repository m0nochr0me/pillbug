# Pillbug Native Audio Plan

Exploration date: 2026-06-14 · Baseline: `master` @ `2df4ab1`

This document records the feasibility exploration for native audio over the websocket channel
and a phased plan to act on it. As with the other plans in `doc/`, each phase is independently
shippable and ends with the same verification gate: `uv run pytest` green, `uv run ruff check .`
clean, and no change to existing public behavior (tool names, HTTP routes, `PB_` env contract,
the text-only websocket wire format used by current clients).

---

## The core finding: two features, opposite sides of an architectural line

"Native audio" splits into two unrelated efforts. They share almost no implementation.

| | `gemini-3.1-flash-lite` | `gemini-3.1-flash-live-preview` |
| --- | --- | --- |
| Audio **in** | ✅ as a normal multimodal part | ✅ streamed |
| Audio **out** | ❌ text only | ✅ native audio-to-audio |
| API surface | regular `generateContent` / `streamGenerateContent` | **Live API** (`BidiGenerateContent`, persistent bidi websocket to Google) |
| Fits current runtime flow | yes, ~80% already built | no — new transport |

**Audio input is mostly already built.** The core AI path already accepts `audio/*`:

- `app/core/ai/attachments.py:67` — `_normalize_supported_attachment_mime_type` passes through
  any `audio/*` MIME type.
- `app/core/ai/session.py:344-359` — attachments ≤ `_INLINE_ATTACHMENT_MAX_BYTES` (8 MB,
  `attachments.py:26`) are inlined as `types.Part.from_bytes`; larger ones go through
  `files.upload` (`session.py:361-384`).
- `app/core/ai/attachments.py:145` — `_extract_inbound_attachments` consumes the generic
  `inbound_attachments` metadata list that any channel can emit.

So an audio-capable model can already understand a voice clip the moment a channel hands it over
as an `inbound_attachments` entry. No AI-layer change is required for Tier 1.

---

## Open decisions (settle before Phase 1)

1. **Backend.** ✅ **Decided (2026-06-14): real Gemini only; a configured proxy fails fast.**
   Tier 1 audio understanding requires the **real Gemini backend**. Both `pillbug-genai-proxy`
   (`app.py:198`) and `pillbug-claude-api-proxy` (`app.py:128`) **501 on file uploads**, and their
   Claude/OpenAI-compatible upstreams cannot do Gemini-native audio understanding regardless.
   Phase 1 therefore **rejects audio with an `error` event when `PB_GEMINI_BASE_URL` is set**,
   rather than forwarding audio a proxy cannot interpret. Text sessions over the same websocket
   continue to work behind a proxy unchanged.
2. **Scope.** Audio-in → text-out (Phase 1) is small and contained. Full voice-in/voice-out is
   the Live API (Phase 3), a substantial new transport. This plan treats Phase 1 as the primary
   deliverable, Phase 2 (TTS) as an optional half-duplex middle step, and Phase 3 as a spike.

Because of decision #1, Phase 1 keeps audio **inline-only** (cap at the 8 MB inline threshold) so
it never hits the upload path — which also keeps the door open to proxy-backed deployments for
*non-audio* sessions on the same channel.

---

## Phase 1 — Websocket audio **input** → text out (small, ~80% built) — ✅ shipped 2026-06-14

**Status.** Implemented at `master` working tree: audio ingress + `_handle_audio_message` in
`packages/pillbug-websocket/.../websocket_channel.py`, `PB_WEBSOCKET_MAX_AUDIO_BYTES` (default
8 MB) in the websocket config, the `websocket → inbox/websocket` default root in
`app/core/config.py`, the fail-fast proxy guard, and `tests/unit/packages/test_websocket_audio.py`
(6 tests). Full suite green (481), `ruff check` clean. Steps below record what was built.

**Goal.** A websocket client can send a short voice clip; the runtime forwards it to Gemini as a
multimodal part and replies with text (or streamed text, unchanged). Reuses the entire existing
attachment path; the only new code is websocket ingress + one config default.

**Steps.**

1. **Accept an audio payload in `_on_message`** (`websocket_channel.py:231`). Today
   `_extract_message_text` (`:257`) reads only `str` or `{"text": ...}` and silently drops
   everything else. Extend the `message` handler to also recognize an audio payload, e.g.
   `{"audio": {"data": "<base64>", "mime_type": "audio/webm", "filename": "clip.webm"},
   "text": "<optional caption>"}`. Socket.IO also supports raw binary frames; base64-in-dict is
   chosen because it carries the MIME type and caption inline and matches the existing
   dict-shaped `message` contract. Keep plain-text messages working unchanged (backward compat).
2. **Validate and persist.** Decode base64; reject if not `audio/*` or over the size cap; write
   under `WORKSPACE_ROOT/inbox/websocket/<session_ulid>-<monotonic_ts>.<ext>` using the shared
   async write helper in `app/util/workspace.py`. Resolve through `resolve_path_within_root` so
   the write stays inside the sandbox.
3. **Emit the attachment.** Put an `InboundMessage` on the queue (`websocket_channel.py:243`)
   whose `metadata["inbound_attachments"]` mirrors the telegram shape
   (`telegram_channel.py:691`):
   ```python
   "inbound_attachments": [
       {"path": workspace_path, "mime_type": mime_type,
        "display_name": filename, "source": "websocket", "kind": "audio"}
   ]
   ```
   Carry any caption as the message `text`; use a non-empty placeholder (e.g. the filename) when
   there is no caption so the message is not dropped as empty.
4. **Register the per-channel inbox sub-root.** Add `"websocket": "inbox/websocket"` to
   `_DEFAULT_INBOUND_ATTACHMENT_ROOTS` (`app/core/config.py:42`). Without it,
   `resolve_inbound_attachment_path` (`attachments.py:122-142`) falls back to the workspace root
   — adding the entry makes the sandbox boundary tight and matches telegram/a2a/cli.
5. **New websocket settings** (`pillbug_websocket/config.py`, prefix `PB_WEBSOCKET_`):
   - `MAX_AUDIO_BYTES: int` — default ≤ 8 MB so audio always inlines (never hits the
     upload/501 path). Reject larger clips with a client-facing error event.
   - `ACCEPTED_AUDIO_MIME: str` — optional CSV allowlist (default permissive `audio/*`).
6. **Reject cleanly when over cap / wrong type.** Emit an error back to the originating `sid`
   (e.g. an `error` event) rather than silently dropping, so clients get feedback.

**Out of Phase 1 (note, don't build):** inbox retention/cleanup. Written clips accumulate under
`inbox/websocket/` exactly as telegram downloads do today; if growth matters, add a janitor in a
follow-up rather than coupling it to this change.

**Verify.**
- Unit: `_on_message` decodes a base64 audio payload, writes it under `inbox/websocket`, and
  emits an `InboundMessage` with a well-formed `inbound_attachments` entry; oversized and
  non-audio payloads are rejected with an error event; plain-text messages still flow unchanged.
- Unit: `inbound_attachment_roots()` includes `websocket → inbox/websocket`.
- Integration: an `InboundMessage` carrying a `websocket` audio attachment resolves to a Gemini
  `Part` via the existing `session.py` path (the inline branch, given the cap).
- `uv run pytest` green, `uv run ruff check .` clean.

---

## Phase 2 — Outbound audio via TTS (optional, half-duplex)

**Goal.** Speak the text reply without adopting the Live API. A dedicated TTS model (e.g.
`gemini-2.5-flash-preview-tts`) converts the finished text turn into an audio file delivered as an
outbound attachment. Stays inside the turn-based flow; no streaming-protocol change.

**Why it's separate.** `WebSocketChannel.send_message` currently discards attachments
(`websocket_channel.py:93`, `del metadata, attachments`). Outbound audio means actually
delivering an `OutboundAttachment` over socket.io (binary frame or base64 event), which is its own
small feature. Also still requires the real Gemini backend.

**Sketch (not detailed until Phase 1 lands).**
1. Add an opt-in (`PB_WEBSOCKET_TTS_*` or a runtime setting) to synthesize the reply text.
2. Teach `send_message` / `send_response` to emit an audio attachment event alongside the text.
3. Decide where synthesis lives — most likely a small outbound step, keeping the core
   `GeminiChatSession` text contract intact.

**Verify.** Round-trip test: text reply → TTS attachment event received by a fake client.

---

## Phase 3 — Native audio-to-audio via the Live API (spike, experimental)

**Goal.** Real voice-in/voice-out with `gemini-3.1-flash-live-preview`.

**Why it does not fit the current architecture.** The Live API is `BidiGenerateContent`: a
long-lived bidirectional websocket session to Google with server-side VAD, barge-in/interruption,
and continuous PCM both directions. The runtime today is strictly turn-based:
`channel → debounce → InboundProcessingPipeline → GeminiChatSession.send_message → one reply`
(per the runtime message flow in `AGENTS.md`). The Live session has no debounce, no per-turn
request, and no `streamGenerateContent`. The proxies do **not** implement it at all — this path
must talk **directly to Google**, bypassing the proxy entirely.

**Shape of the work (spike, decide go/no-go before committing).**
1. A new **live transport/mode** that bridges the client's socket.io connection directly to a
   Google `live.connect()` session via `google-genai`, streaming PCM frames both ways.
2. It sidesteps the debounce buffer, the inbound pipeline, and the per-turn `GeminiChatSession`
   — so it needs its own session lifecycle, its own tool-exposure story (if any), and its own
   correlation with `register_channel_conversation` for telemetry.
3. Open unknowns to resolve in the spike: how MCP tools (if needed) attach to a Live session;
   how planning-mode / security checks apply to a continuous stream; how this coexists with the
   existing text websocket on the same port; cost/latency under the user's network path; and how
   session summarization (which assumes stored Gemini history) interacts with a Live session.

**Verify.** A throwaway end-to-end demo (mic → Live session → speaker) against the real Gemini
backend, plus a written go/no-go with the architectural decisions above resolved. No production
wiring until that decision is made.

---

## Deliberately not in scope

- **Routing audio through the proxies.** The proxies translate to Claude/OpenAI upstreams and
  501 on uploads; Gemini-native audio understanding and the Live API are both out of reach there
  by design. Audio features require the real Gemini backend.
- **Changing the inline/upload split or the 8 MB threshold** in `session.py`. Phase 1 caps audio
  below it deliberately; the existing path is unchanged.
- **A generic binary-upload channel.** Phase 1 is audio-specific to keep validation tight; widen
  it only if another media type actually needs the websocket.
- **Bundling Phase 3 with Phase 1.** They share no code. Ship audio-in first; treat the Live API
  as a separate, decision-gated project.
