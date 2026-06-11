# pillbug-claude-api-proxy

A FastAPI service that exposes the Gemini `generateContent` wire format on top of the official [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python). Authenticates with the Claude Code subscription OAuth token (`CLAUDE_CODE_OAUTH_TOKEN`), so calls are billed against your **Claude Pro/Max subscription** and driven directly through the Messages API — Gemini history (including prior tool round-trips) reaches the model as native `tool_use` / `tool_result` blocks end-to-end, with no agent-loop priming or transcript-in-system-prompt bridge.

Sibling of `pillbug-genai-proxy`: same Gemini wire-format entry, different upstream.

## Install

```bash
uv sync --extra claude_api_proxy
```

Mint a Claude Code subscription OAuth token (a long-lived bearer token tied to your Claude Pro/Max subscription):

```bash
claude setup-token
```

Capture the printed token into `CLAUDE_CODE_OAUTH_TOKEN` (or the Pillbug-prefixed override below).

## Configure

```bash
export CLAUDE_CODE_OAUTH_TOKEN=<paste from claude setup-token>
# or, equivalently:
# export PB_CLAUDE_API_PROXY_OAUTH_TOKEN=<token>

export PB_CLAUDE_API_PROXY_HOST=127.0.0.1
export PB_CLAUDE_API_PROXY_PORT=9033
export PB_CLAUDE_API_PROXY_MODEL=claude-sonnet-4-6   # optional, overrides the model from the request URL
export PB_CLAUDE_API_PROXY_MAX_TOKENS=8192           # default output cap if generationConfig doesn't supply one
```

Run the proxy:

```bash
uv run pillbug-claude-api-proxy
```

Point a Pillbug runtime at it (no Pillbug code changes — reuses `PB_GEMINI_BASE_URL`):

```bash
export PB_GEMINI_BACKEND=developer
export PB_GEMINI_API_KEY=dummy
export PB_GEMINI_BASE_URL=http://127.0.0.1:9033
export PB_GEMINI_MODEL=claude-sonnet-4-6
```

## How it works

Each `:generateContent` request becomes one Anthropic Messages API call:

1. Gemini `contents[]` is converted into Anthropic `messages` with structured `text` / `image` / `tool_use` / `tool_result` content blocks. Prior tool round-trips reach the model as native blocks — no transcript-in-system-prompt bridge, because the Messages API accepts the full structured history directly.
2. Gemini `functionDeclarations` become Anthropic `tools` entries (`{name, description, input_schema}`). Tool execution stays on Pillbug's side: the proxy only relays the model's `tool_use` intent back as a Gemini `functionCall`.
3. `generationConfig.{temperature, topP, maxOutputTokens, stopSequences}` map onto the Anthropic sampling params.

Auth and the Claude Code subscription path:

- The OAuth token is passed via the SDK's `auth_token` argument (`Authorization: Bearer …`), distinct from API-key auth (`x-api-key`). The `auth_token` parameter is officially supported by the anthropic Python SDK.
- The system prompt is prefixed with `You are Claude Code, Anthropic's official CLI for Claude.`; the user's `systemInstruction` follows the prefix.

**The system-prompt prefix is load-bearing.** Empirically verified against `api.anthropic.com` on 2026-05-26: requests authenticated with `CLAUDE_CODE_OAUTH_TOKEN` that lack this prefix are rejected with `HTTP 429 rate_limit_error: "Error"` (abuse rejection masquerading as a rate limit; the message body is just the literal word "Error" with no quota details). Requests that include it succeed with HTTP 200 against the same token. Setting `PB_CLAUDE_API_PROXY_CLAUDE_CODE_SYSTEM_PREFIX=` (empty) will make every call fail; the proxy logs a warning at startup if the prefix is empty.

The `anthropic-beta: oauth-2025-04-20` header — frequently cited in community subscription-bridge projects — turned out to be **irrelevant** in the same test: with the prefix present, requests succeed equally with or without the beta header. The header knob is left configurable for forward-compatibility but defaults to empty (no header sent). If a future contract change makes one necessary, set:

- `PB_CLAUDE_API_PROXY_OAUTH_BETA_HEADER=oauth-2025-04-20` (or whatever value the current `claude` CLI sends).

None of this is documented by Anthropic; treat the OAuth-as-third-party-API path as unofficial.

## Audio input

Claude has no native audio modality — the Anthropic Messages API has no audio content block (current models accept only text and image input). Pillbug forwards voice notes as Gemini `inlineData` audio parts, so the proxy rewrites them before history translation. `PB_CLAUDE_API_PROXY_AUDIO_MODE` controls how:

- `placeholder` (default): each audio part becomes a short text note so the model knows a voice message arrived but was not transcribed. No API key required.
- `elevenlabs`: inline audio is transcribed via [ElevenLabs Scribe](https://elevenlabs.io/docs/api-reference/speech-to-text/convert) (`model_id=scribe_v2`) and replaced with the transcript text. Any failure (no key, API error, empty result) falls back to the placeholder.

The ElevenLabs key resolves the same way as the `text-to-speech` skill: the Docker/Kubernetes secret file `/run/secrets/elevenlabs_api_key` first, then the `ELEVENLABS_API_KEY` environment variable. Transcripts are cached by audio content hash, so the same clip re-sent across conversation turns is billed only once. Only inline audio (≤8 MiB, sent as `Part.from_bytes`) can be transcribed; larger files arrive as a Gemini file URI the proxy cannot fetch and always become a placeholder.

## Coverage

Translates the subset of the Gemini wire format Pillbug actually sends:

- `systemInstruction`, text and `inlineData` (image, base64) parts, function declarations, function call/response parts.
- `generationConfig.{temperature, topP, maxOutputTokens, stopSequences}` → Anthropic sampling params.
- `usage` (from `Message.usage.input_tokens` / `output_tokens`) → `usageMetadata`.
- Streaming: `:streamGenerateContent?alt=sse` re-frames the Anthropic event stream as Gemini SSE chunks. Text deltas stream incrementally; `tool_use` blocks are buffered whole and emitted as `functionCall` parts on the final chunk, which also carries `finishReason` and `usageMetadata`.

Out of scope:

- File uploads (`/upload/v1beta/files` returns 501).
- Tool execution stays on Pillbug's side — the proxy only relays the model's `tool_use` intent.
- `thinkingConfig` (dropped — Pillbug doesn't use Anthropic extended thinking through this path).

## Configuration reference

| Env var | Default | Notes |
| --- | --- | --- |
| `PB_CLAUDE_API_PROXY_HOST` | `127.0.0.1` | |
| `PB_CLAUDE_API_PROXY_PORT` | `9033` | Distinct from `pillbug-genai-proxy` (9031). |
| `PB_CLAUDE_API_PROXY_MODEL` | (empty) | Overrides the model name from the request URL. |
| `PB_CLAUDE_API_PROXY_MAX_TOKENS` | `8192` | Cap on output tokens when `generationConfig.maxOutputTokens` is not supplied. |
| `PB_CLAUDE_API_PROXY_REQUEST_TIMEOUT_SECONDS` | `600.0` | |
| `PB_CLAUDE_API_PROXY_OAUTH_TOKEN` | (empty) | Bearer OAuth token from `claude setup-token`. Falls back to `CLAUDE_CODE_OAUTH_TOKEN`. |
| `PB_CLAUDE_API_PROXY_CLAUDE_CODE_SYSTEM_PREFIX` | `You are Claude Code, Anthropic's official CLI for Claude.` | Prepended to `systemInstruction`. **Required by the OAuth subscription path**; setting empty triggers HTTP 429 on every call. |
| `PB_CLAUDE_API_PROXY_OAUTH_BETA_HEADER` | (empty) | If set, sent as the `anthropic-beta` request header. Not currently required by the OAuth path; reserved for forward-compatibility. |
| `PB_CLAUDE_API_PROXY_AUDIO_MODE` | `placeholder` | `placeholder` or `elevenlabs` — how inbound audio is handled (Claude has no audio modality). See **Audio input**. |
| `PB_CLAUDE_API_PROXY_ELEVENLABS_MODEL` | `scribe_v2` | ElevenLabs Scribe model id used when `AUDIO_MODE=elevenlabs`. |
| `PB_CLAUDE_API_PROXY_ELEVENLABS_BASE_URL` | `https://api.elevenlabs.io` | ElevenLabs API base URL. |
| `ELEVENLABS_API_KEY` | (empty) | ElevenLabs Scribe key for `AUDIO_MODE=elevenlabs`. Resolved from `/run/secrets/elevenlabs_api_key` first, then this env var (no `PB_`-prefixed override — it is shared infrastructure). |
| `PB_CLAUDE_API_PROXY_LOG_INCLUDE_TRACEBACK` | `false` | |
