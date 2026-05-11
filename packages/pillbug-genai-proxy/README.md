# pillbug-genai-proxy

A small FastAPI service that exposes the Gemini `generateContent` wire format on top of an OpenAI-compatible chat completions endpoint (llama.cpp, vLLM, LiteLLM, Ollama, etc.). Lets a Pillbug runtime drive a local model through the existing `google-genai` SDK without forking the AI client.

## Install

```bash
uv sync --extra genai_proxy
```

## Configure

```bash
export PB_GENAI_PROXY_HOST=127.0.0.1
export PB_GENAI_PROXY_PORT=9000
export PB_GENAI_PROXY_UPSTREAM_URL=http://127.0.0.1:8080/v1
export PB_GENAI_PROXY_UPSTREAM_API_KEY=anything-or-empty
export PB_GENAI_PROXY_UPSTREAM_MODEL=gemma-3-12b   # optional override
```

Run the proxy:

```bash
uv run pillbug-genai-proxy
```

Point a Pillbug runtime at it:

```bash
export PB_GEMINI_BACKEND=developer
export PB_GEMINI_API_KEY=dummy
export PB_GEMINI_BASE_URL=http://127.0.0.1:9000
export PB_GEMINI_MODEL=gemma-3-12b
```

## Coverage

Translates the subset of the Gemini wire format Pillbug actually sends:

- `systemInstruction`, text and `inlineData` (image) parts, function declarations, function call/response parts.
- `generationConfig.{temperature, topP, maxOutputTokens}` → OpenAI sampling params.
- `usage` → `usageMetadata`.

Out of scope: streaming (`:streamGenerateContent` returns 501), file uploads (`/upload/v1beta/files` returns 501), `thinkingConfig` (dropped — AFC is client-side anyway).
