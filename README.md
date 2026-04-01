# Pillbug

<p align="center"><img src="app/assets/pillbug_logo.svg" alt="Pillbug logo" width="220"></p>

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg?logo=github&logoColor=white)](https://opensource.org/licenses/MIT)
[![Standardized Agent Exams: 100%](https://img.shields.io/badge/SAE-100%25-teal.svg?logo=github&logoColor=white)](https://www.kaggle.com/experimental/sae/5dc91772-3e30-efa6-f325-22fd928212d6)

Pillbug is an async AI agent runtime built for isolated deployment.

## Highlights

- One agent, one runtime, and one workspace per container
- Async runtime with debounced inbound message handling
- Native audio recognition and vision support via multi-modal Gemini API
- Local MCP server for workspace file, search, command, outbound channel, and URL-fetching tools
- Built-in session commands, summarization, and session-scoped planning
- Embedded scheduler for background agent tasks
- Workspace skill discovery from `skills/*/SKILL.md`
- Optional A2A, Telegram, and dashboard packages
- Per-workspace `AGENTS.md` instructions seeded on first run

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

Pillbug has not bundled memory management features intentionally to allow users to choose their preferred approach.

[Arca-Memory](https://github.com/arca-mem/arca-memory) is a *recommended* compatible external memory management service that can be easily integrated via the MCP tools API.

Users can also implement custom memory management solutions by using agent skills or other MCP servers.

## Limitations

- Currently only supports Gemini API for agent interactions. Support for additional LLM providers may be added in the future based on demand.
- Only HTTP MCP servers are supported at this time.
- So far only Telegram is supported as a non-CLI channel, but additional channels can be added via the plugin system.
