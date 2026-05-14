# Pillbug

<p align="center"><img src="app/assets/pillbug_logo.svg" alt="Pillbug logo" width="220"></p>

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg?logo=github&logoColor=white)](https://opensource.org/licenses/MIT)
[![Standardized Agent Exams: 100%](https://img.shields.io/badge/SAE-100%25-teal.svg?logo=github&logoColor=white)](https://www.kaggle.com/experimental/sae/5dc91772-3e30-efa6-f325-22fd928212d6)

Pillbug is an async AI agent runtime built for isolated deployment.

## Why Pillbug?

Pillbug is opinionated about one thing: **one agent, one runtime, one workspace, per container.** Everything else — Gemini-first, MCP-native, plugin channels, bring-your-own-memory — follows from that.

Pick Pillbug when you want:

- **Strict per-tenant isolation.** Each runtime has its own workspace, identity, and security boundary. No multi-tenant routing inside the process.
- **A workspace-sandboxed tool surface.** File reads, edits, search, command execution, scheduling, and URL fetches all live behind a local MCP server scoped to `WORKSPACE_ROOT`.
- **A composable channel model.** CLI, Telegram, Matrix, WebSocket, A2A, HTTP trigger — each is a plugin package, registered through env config, not hardcoded into the loop.
- **Production posture out of the box.** Non-root container PID 1, bearer-protected control plane, reloadable security patterns, structured JSON logs.
- **Backend flexibility without core churn.** Gemini developer or Vertex natively; llama.cpp, vLLM, Ollama, LiteLLM via the bundled OpenAI-compatibility proxy.

Pillbug is probably **not** the right fit if you need multi-agent routing inside a single process, bundled memory, voice or video output flows, or a one-file demo. It is deliberately a runtime, not a platform.

## Highlights

- One agent, one runtime, and one workspace per container
- Async runtime with debounced inbound message handling
- Native audio recognition and vision support via multi-modal Gemini API
- Gemini developer and Vertex AI backends, plus OpenAI-compatible upstreams (llama.cpp, vLLM, Ollama, LiteLLM) through the bundled translation proxy
- Local MCP server for workspace file, search, command, outbound channel, and URL-fetching tools
- Built-in session commands, summarization, and session-scoped planning
- Embedded scheduler for background agent tasks
- Workspace skill discovery from `skills/*/SKILL.md`
- Optional channel and integration packages: A2A, Telegram, Slack, Matrix, WebSocket (Socket.IO), HTTP trigger, dashboard, bundled memory, and the OpenAI-compatibility proxy
- Per-workspace `AGENTS.md` instructions seeded on first run

## Quickstart

The fastest *is this real?* path. Requires Python 3.14+, [uv](https://docs.astral.sh/uv/), and a Gemini API key.

```bash
git clone https://github.com/m0nochr0me/pillbug.git
cd pillbug
uv sync --locked
export PB_GEMINI_API_KEY=your_api_key
./run.sh
```

The first run **exits intentionally** after seeding `~/.pillbug/workspace/AGENTS.md` and the runtime identity. This is normal — edit that file to set your agent persona, then run `./run.sh` again to start the CLI channel.

For Docker, multi-runtime setups, optional channel extras, or external memory, see [doc/INSTALL.md](doc/INSTALL.md).

## Docs

*Ask your existing coding agent to follow the installation instructions and deploy a runtime!*

- Installation: [doc/INSTALL.md](doc/INSTALL.md)
- Configuration reference: [doc/CONFIGURATION.md](doc/CONFIGURATION.md)
- Example deployment files: `doc/simple/` and `doc/multi/`

## Architecture

```mermaid
flowchart LR
  Input[User or external system] --> Channels[Channel plugins]
  Channels --> Loop[ApplicationLoop]
  Loop --> Debounce[Per-session debounce buffer]
  Debounce --> Pipeline[InboundProcessingPipeline]
  Pipeline -->|blocked| Reject[Security rejection]
  Reject --> Reply[Send response]
  Pipeline -->|accepted| Session[GeminiChatSession]
  Session --> Context[Base context + AGENTS.md + skills + channel memos]
  Session --> Binding[Session binding metadata]
  Session --> MCP[Local MCP server]
  MCP --> Control[Telemetry + control + Agent Card APIs]
  MCP --> Workspace[Scoped workspace file and command tools]
  Session --> Gemini[Gemini API]
  Context --> Gemini
  Control --> Dashboard[Operator dashboard or peers]
  Gemini --> Reply
  Reply --> Channels
```

## Memory Management

Memory lives outside core so each runtime can pick the tier that fits. Three options ship in-tree:

- **Bundled (simple)** — install the `memory` extra for flat-Markdown CRUD under `workspace/memory/` exposed through five MCP tools (`memory_list`, `memory_get`, `memory_add`, `memory_update`, `memory_delete`). Stdlib-only, no database. Good default for single-runtime setups. See [packages/pillbug-memory](packages/pillbug-memory).
- **External (graph + semantic)** — [Arca-Memory](https://github.com/arca-mem/arca-memory) is a recommended compatible MCP service for buckets, semantic search, and graph traversal. Wire it in through `mcp.json`.
- **Custom** — bring any other MCP server, or implement memory inside a workspace skill.

## Optional Packages

Workspace members under `packages/` are installed through uv extras and registered as channel plugins or standalone services. See [doc/INSTALL.md](doc/INSTALL.md) and each package README for details.

| Extra | Package | Purpose |
| - | - | - |
| `a2a` | [pillbug-a2a](packages/pillbug-a2a) | Agent-to-agent HTTP channel with peer discovery |
| `telegram` | [pillbug-telegram](packages/pillbug-telegram) | Telegram bot channel |
| `slack` | [pillbug-slack](packages/pillbug-slack) | Slack channel over Socket Mode (no public HTTP endpoint required) |
| `matrix` | [pillbug-matrix](packages/pillbug-matrix) | Matrix channel with attachment, voice-message, and typing support |
| `websocket` | [pillbug-websocket](packages/pillbug-websocket) | Socket.IO channel keyed by client-provided ULID session IDs |
| `trigger` | [pillbug-trigger](packages/pillbug-trigger) | HTTP ingress for external event sources with per-source prompt templates |
| `dashboard` | [pillbug-dashboard](packages/pillbug-dashboard) | Operator dashboard service |
| `genai_proxy` | [pillbug-genai-proxy](packages/pillbug-genai-proxy) | Gemini wire-format proxy that fronts any OpenAI-compatible chat completions endpoint |
| `memory` | [pillbug-memory](packages/pillbug-memory) | Bundled flat-file Markdown memory store with five MCP tools, rooted in `workspace/memory/` |

The `gmail` extra is a skill-side extra (not a workspace package): it installs the Google API client deps consumed by the bundled [skills/gmail](skills/gmail) workspace skill.

## Bundled Skills

Pillbug ships eight workspace skills under [skills/](skills). The runtime auto-discovers them when they're copied into `<runtime-base>/workspace/skills/` (see [doc/INSTALL.md](doc/INSTALL.md)); each one is a directory with a `SKILL.md` whose frontmatter Pillbug parses at startup.

| Skill | Purpose |
| - | - |
| [arca-memory](skills/arca-memory) | Operating guide for the Arca-Memory MCP service (buckets, semantic search, graph traversal) |
| [bluesky](skills/bluesky) | Publish posts to Bluesky |
| [feed-reader](skills/feed-reader) | RSS/Atom subscriptions: list new posts, fetch full text, manage feed lists |
| [financial-assistant](skills/financial-assistant) | Track personal expenses to a CSV ledger with live exchange-rate normalization |
| [gmail](skills/gmail) | Read Gmail mailboxes via a service account with domain-wide delegation (read-only) |
| [tavily-search](skills/tavily-search) | Web search through the Tavily API, with a bundled shell wrapper |
| [text-to-speech](skills/text-to-speech) | Synthesize speech from text via ElevenLabs |
| [threads](skills/threads) | Publish posts to Threads (Meta) |

Most skills need their own credentials (Bluesky app password, Tavily API key, ElevenLabs key, Gmail service account, etc.) — see each skill's `SKILL.md`. The `gmail` skill additionally requires `uv sync --extra gmail` for the Google API client deps. Drop unwanted skills before copying, or remove them from the runtime workspace at any time.

## OpenAI-compatible Backends

Pillbug speaks the Gemini wire format directly, but the `pillbug-genai-proxy` extra ships a small FastAPI translator that exposes `POST /v1beta/models/{model}:generateContent` and forwards translated requests to an OpenAI-compatible upstream (llama.cpp, vLLM, LiteLLM, Ollama, etc.). Point the runtime at the proxy with `PB_GEMINI_BASE_URL` and keep the rest of the Gemini-first chat session, MCP tools, and AFC behavior unchanged. See [packages/pillbug-genai-proxy/README.md](packages/pillbug-genai-proxy/README.md) for the supported translation surface.

## Limitations

- Streaming (`:streamGenerateContent`) and Gemini file uploads are not translated by the OpenAI-compatibility proxy.
- Only HTTP MCP servers are supported at this time.
- Matrix support currently runs without end-to-end encryption.
