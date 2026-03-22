# Pillbug Implementation Plan

This plan reflects Pillbug's intended direction as an isolation-first agent runtime rather than a shared multi-agent platform.

## Goal

Preserve one agent, one runtime, and one workspace per container while adding:

- explicit A2A communication between runtimes when needed
- telemetry endpoints for external observability
- narrow control endpoints for a separate dashboard application

## Core Principles

- Isolation is the default deployment model
- Federation is explicit and network-based
- Operator UX lives outside the runtime container
- Runtime internals stay useful without any dashboard attached
- Shared workspaces and shared session state are out of scope

## Non-Goals

- Running multiple cooperating agents inside one Pillbug process
- Turning the runtime into a bundled web application
- Making A2A equivalent to arbitrary remote tool execution
- Adding shared workspace storage across runtimes
- Hiding cross-runtime boundaries behind a fake single-session model

## Target Topology

```text
+-------------------+      +-------------------+
| pillbug runtime A |<---->| pillbug runtime B |
| agent + workspace | A2A  | agent + workspace |
+-------------------+      +-------------------+
          ^                            ^
          | telemetry/control          | telemetry/control
          v                            v
      +------------------------------------+
      | separate dashboard container       |
      | web UI + node registry + auth      |
      +------------------------------------+
```

## Phase 1: Runtime Identity And Auth Foundations

Add the minimum configuration and schema needed for isolated runtimes to be addressable and secure.

Work:

- Add a stable runtime identifier such as `PB_RUNTIME_ID`
- Add auth settings for dashboard access and A2A peer access
- Define capability separation between dashboard tokens and A2A tokens
- Add Pydantic models for runtime metadata, auth scope, and operator responses

Suggested code areas:

- `app/core/config.py`
- `app/schema/telemetry.py` new file
- `app/schema/control.py` new file

Acceptance criteria:

- Every runtime exposes a stable identity
- Runtime startup fails clearly on invalid auth configuration
- Control and A2A traffic can be scoped independently

## Phase 2: Telemetry Endpoints

Expose read-only operational state for external dashboards. Start simple and make the data model stable before adding more controls.

Initial endpoints:

- `GET /health`
- `GET /telemetry/runtime`
- `GET /telemetry/channels`
- `GET /telemetry/sessions`
- `GET /telemetry/tasks`
- `GET /telemetry/events`

Recommended payload fields:

- runtime id
- agent name if configured
- uptime
- enabled channels
- active session count
- known channel destinations
- scheduler state
- task counts and recent task runs
- recent errors and last activity timestamps

Implementation notes:

- Use Server-Sent Events for `GET /telemetry/events` before considering WebSockets
- Keep telemetry read-only
- Mount these routes alongside the existing FastAPI app rather than through MCP tools

Suggested code areas:

- `app/mcp.py`
- `app/runtime/loop.py`
- `app/runtime/scheduler.py`
- `app/core/telemetry.py` new file

Acceptance criteria:

- A separate dashboard container can poll runtime status without using MCP
- Basic runtime activity can be streamed over SSE
- No telemetry endpoint leaks workspace contents by default

## Phase 3: Narrow Control Endpoints

Add operator actions that are explicit, auditable, and small in scope.

Initial endpoints:

- `POST /control/sessions/{session_id}/clear`
- `POST /control/messages/send`
- `POST /control/tasks/{task_id}/enable`
- `POST /control/tasks/{task_id}/disable`
- `POST /control/tasks/{task_id}/run-now`
- `POST /control/runtime/drain`
- `POST /control/runtime/shutdown`

Implementation notes:

- Require auth on every control route
- Log every control action with runtime id and caller scope
- Avoid a generic shell or filesystem operator endpoint
- Prefer explicit commands over a broad admin API

Suggested code areas:

- `app/mcp.py`
- `app/runtime/loop.py`
- `app/runtime/scheduler.py`
- `app/schema/control.py` new file

Acceptance criteria:

- The dashboard can perform common operations without touching MCP
- Control actions are visible in structured logs
- Unsafe generic remote execution is not introduced

## Phase 4: A2A Channel MVP

Implement [A2A Protocol](https://a2a-protocol.org/latest/specification/) as a channel plugin so cross-runtime collaboration stays aligned with the existing message pipeline.

Channel behavior:

- inbound HTTP requests are converted into `InboundMessage` values
- the channel buffers inbound envelopes through an async queue
- `listen()` yields A2A messages into the normal application loop
- `send_message()` delivers outbound envelopes to peer runtimes
- responses remain message-oriented rather than tool-oriented

Envelope fields:

- sender runtime id
- sender agent name
- target runtime id
- conversation id
- message id
- reply to message id
- intent
- text
- metadata
- attachments

Recommended intents:

- `ask`
- `inform`
- `delegate`
- `result`
- `error`
- `heartbeat`

Implementation notes:

- Keep A2A separate from dashboard control APIs
- Do not merge remote and local sessions into one hidden shared context
- Validate runtime id, auth, and envelope schema before enqueueing messages
- Treat A2A as optional and package-friendly, similar to Telegram

Suggested code areas:

- `packages/pillbug-a2a/` new workspace package
- `app/runtime/channels.py`
- `app/schema/messages.py`
- `app/mcp.py` or a dedicated HTTP module for A2A ingress

Acceptance criteria:

- Runtime A can send a message to runtime B through A2A
- Runtime B processes the message through the normal pipeline
- Operator APIs and A2A traffic use distinct auth scopes

## Phase 5: Separate Dashboard App

Build the dashboard as an independent web application that treats Pillbug runtimes as remote nodes.

Dashboard responsibilities:

- register known runtimes
- poll or subscribe to telemetry
- display runtime health, sessions, tasks, and recent events
- trigger approved control actions

Dashboard non-responsibilities:

- owning agent logic
- storing workspace state for the runtime
- replacing the runtime's MCP surface

Suggested deliverable order:

1. runtime list and health view
2. task and session detail pages
3. control actions with audit trail
4. A2A topology view

## File-Level Impact Summary

- `app/core/config.py`: runtime identity, auth, and endpoint settings
- `app/mcp.py`: telemetry and control routes, possibly A2A ingress wiring
- `app/runtime/loop.py`: event emission for session lifecycle and message handling
- `app/runtime/scheduler.py`: telemetry for tasks and task control hooks
- `app/runtime/channels.py`: plugin registration remains the extension seam for A2A
- `app/schema/messages.py`: A2A envelope metadata conventions if needed
- `app/schema/telemetry.py`: runtime telemetry models
- `app/schema/control.py`: control request and response models
- `packages/pillbug-a2a/`: optional A2A channel package

## Milestone Order

1. Runtime identity and auth settings
2. Read-only telemetry endpoints and SSE stream
3. Narrow control endpoints
4. A2A channel MVP
5. Separate dashboard container

## Success Criteria

- Pillbug remains one agent, one runtime, and one workspace per container
- Cross-runtime collaboration is possible without shared local state
- A dashboard can observe and operate runtimes without embedding into the runtime container
- MCP remains the model tool plane rather than becoming the operator API
- The runtime still works fully in headless deployments
