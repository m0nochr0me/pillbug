# Operator Dashboard Improvements — Plan

Scope: four upgrades to `packages/pillbug-dashboard`, each requiring small
runtime-side additions in `app/mcp.py` (telemetry endpoints + a few new control
actions). No new infrastructure; the dashboard stays server-rendered + Vue
islands with SSE for live updates.

## 0. Current State Baseline

Runtime exposes (today):

- Telemetry: [`/health`](app/mcp.py#L2486), [`/telemetry/runtime`](app/mcp.py#L2492),
  [`/telemetry/channels`](app/mcp.py#L2500), [`/telemetry/sessions`](app/mcp.py#L2511),
  [`/telemetry/tasks`](app/mcp.py#L2517), [`/telemetry/events`](app/mcp.py#L2523) (SSE).
- Control: [`/control/messages/send`](app/mcp.py#L2751),
  [`/control/sessions/{id}/clear`](app/mcp.py#L2571),
  [`/control/sessions/{id}/planning-mode`](app/mcp.py#L2618),
  [`/control/tasks/{id}/enable|disable|run-now`](app/mcp.py#L2805),
  [`/control/approvals/{id}/approve|deny`](app/mcp.py#L2974),
  [`/control/drafts/{id}/commit|discard`](app/mcp.py#L3002),
  [`/control/runtime/drain|shutdown`](app/mcp.py#L3125).

Dashboard proxies a subset of those in
[routes/api.py](packages/pillbug-dashboard/src/pillbug_dashboard/routes/api.py)
and renders one Vue app per detail page in
[runtime_detail.html](packages/pillbug-dashboard/src/pillbug_dashboard/templates/runtime_detail.html)
+ [runtime-detail.js](packages/pillbug-dashboard/src/pillbug_dashboard/static/js/app/runtime-detail.js).

Gaps that drive this plan:

- **Drafts and approvals have no telemetry GET.** They live only in
  `outbound_draft_store` / `approval_store`
  ([app/runtime/approvals.py](app/runtime/approvals.py)) and are surfaced only
  via the `control.draft_created` SSE event. The dashboard cannot enumerate
  pending items on page load.
- **`manage_agent_task` create/update/delete are MCP-only.** No HTTP control
  endpoints exist for full task CRUD ([app/mcp.py:2309](app/mcp.py#L2309)) —
  the dashboard can only enable/disable/run-now.
- **Tracked conversations** are listed by session key but the dashboard cannot
  read the message history of any session.
- **Send-message form** is a tiny textarea panel — fine for a fire-and-forget
  poke, wrong shape for an interactive chat.

A Socket.IO channel exists at [packages/pillbug-websocket](packages/pillbug-websocket/)
under channel name `websocket`. Its presence is already discoverable per
runtime through `detail.channels.enabled_channels`.

---

## 1. Drafts Section on Runtime Page

Goal: list pending outbound drafts and pending command approvals for a runtime;
operator can approve/commit or deny/discard with optional comment.

### Runtime changes

Add two read-only telemetry endpoints (bearer-protected, same
`_authorize_telemetry` gate as the other `/telemetry/*` GETs):

- `GET /telemetry/drafts` — returns
  `{ outbound: [OutboundDraft.model_dump()...], command: [ApprovalDraft.model_dump()...] }`.
  Filter to `status="pending"` by default; accept `?status=pending|all` for
  future extension. Implementation: `outbound_draft_store.list(status="pending")`
  + `approval_store.list(status="pending")`.
- Optional: `GET /telemetry/drafts/{id}` for fetching a single record (useful
  when the dashboard hydrates a detail modal from an SSE event without a full
  reload).

No new control endpoints needed — commit/discard/approve/deny already exist.

### Dashboard changes

- `services/runtime_client.py`: add `get_drafts(base_url, token)` that GETs
  `/telemetry/drafts` and returns a typed snapshot.
- `services/runtime_hub.py`: fold drafts into `build_detail()` via
  `asyncio.gather`. Add `drafts` field on `RuntimeDetailSnapshot`
  ([schema.py](packages/pillbug-dashboard/src/pillbug_dashboard/schema.py)) as
  `dict[str, Any] | None`. (Keep the schema loose to mirror how `tasks`,
  `sessions`, `channels` are stored — these are pass-through payloads, not
  dashboard-owned models.)
- `routes/api.py`: four new proxy routes —
  - `POST /api/runtimes/{rid}/control/drafts/{draft_id}/commit`
  - `POST /api/runtimes/{rid}/control/drafts/{draft_id}/discard`
  - `POST /api/runtimes/{rid}/control/approvals/{draft_id}/approve`
  - `POST /api/runtimes/{rid}/control/approvals/{draft_id}/deny`

  Each takes an optional `{ "comment": "..." }` body and forwards via
  `_proxy_control_action`.
- `runtime_detail.html` + `runtime-detail.js`: new "DRAFTS" panel above
  SESSIONS, two sub-tables (`OUTBOUND` and `COMMANDS`):
  - Columns for outbound: id (truncated, copyable), kind, channel, target,
    `message` preview (first 80 chars), `created_at`, `created_by`, action
    buttons `COMMIT` / `DISCARD`. Expandable row revealing the full message and
    attachment payload.
  - Columns for command: id, command, cwd, requested_by, action buttons
    `APPROVE` / `DENY`. Expandable row showing argv and rationale.
  - Both actions reuse `PillbugDashboardConfirm` with an optional comment field
    (extend the confirm dialog to accept a free-text input; current dialog only
    has confirm/cancel).
  - Refresh: react to `control.draft_created`, `control.draft_committed`,
    `control.draft_discarded`, `command.approved`, `command.denied` events by
    re-fetching drafts only (lighter than a full detail refresh).
  - Empty state: `NO PENDING DRAFTS`.

### Tests

- Unit test for `runtime_client.get_drafts` happy/error paths.
- Integration test for the four proxy routes (mock httpx).
- Backend: integration test for `/telemetry/drafts` (mix of pending/committed
  records — assert filter).

---

## 2. Chat Section (Websocket-Backed When Available)

Goal: split the current "SEND MESSAGE" textarea into two surfaces:

1. **Quick send** — keeps the existing one-shot form for any channel
   (telegram, cli, a2a). Unchanged semantics, smaller footprint.
2. **Chat** — full conversation view for runtimes that expose the `websocket`
   channel. Opens an interactive Socket.IO session as an operator client,
   showing the model's replies inline.

### Detection rule

In `runtime-detail.js`, compute `chatAvailable = availableChannels.includes("websocket")`.
If false, hide the chat panel entirely and only show Quick Send.

### Auth model: operator-provided token in browser localStorage

Per [CLIENT_INTEGRATION.md](packages/pillbug-websocket/CLIENT_INTEGRATION.md)
the runtime requires `Authorization: Bearer <PB_WEBSOCKET_BEARER_TOKEN>` and
`X-SessionID: <ULID>` **as request headers on the Socket.IO handshake**;
query strings, the `auth` payload, and cookies are explicitly not honored.

Decision: rather than mint short-lived tickets from the dashboard backend,
the operator pastes the websocket bearer they were issued out-of-band into a
per-runtime "WS Token" field; the dashboard stores it in
`localStorage` under a per-runtime key and the Vue panel uses it directly on
the Socket.IO handshake.

- Storage key: `pillbug:ws-token:<runtime_id>` (one token per runtime; no
  cross-runtime leakage).
- The token is never sent to the dashboard backend — the browser talks to the
  runtime's Socket.IO endpoint directly. This is a deliberate departure from
  the existing "dashboard tokens stay server-side" convention
  (memory: 724039af); it applies only to the websocket bearer, not to
  `dashboard_bearer_token`. Trade-off noted explicitly: the operator owns the
  workstation, the token is scoped to one channel on one runtime, and the
  alternative (server-side ticket exchange) adds a runtime endpoint plus a
  rotation story without removing the underlying trust assumption.
- Token UI: a small "WS TOKEN" panel near the chat composer with `Save`,
  `Clear`, and a masked `••••••••` display once saved. Validation is
  empty-vs-non-empty only; the runtime is the authority on the value.

### Browser transport constraint

The browser cannot set custom headers on a native WebSocket upgrade, so the
Socket.IO client must run in `transports: ['polling']` mode (polling XHRs
*can* carry `extraHeaders`). This costs the websocket upgrade but works
without any runtime change. If we later want true websocket transport in the
browser, the runtime would need to honor the Socket.IO `auth` payload — call
that out as a follow-up, not v1 scope.

### Runtime changes

- No mandatory protocol changes. Reuse the existing pillbug-websocket channel.
- Verify (and add if missing) a `public_url` / `connect_url` field on the
  websocket channel details surfaced by `/telemetry/channels`. Without it the
  dashboard would have to guess the runtime's websocket origin (the runtime
  binds on `PB_WEBSOCKET_HOST:PB_WEBSOCKET_PORT`, which may differ from the
  dashboard's runtime `base_url`).
- Optional follow-up (not v1): accept the bearer via the Socket.IO `auth`
  payload as well, so browsers can use the websocket transport.

### Dashboard changes

- No new API proxies — the chat panel talks to the runtime's Socket.IO
  endpoint directly using the operator-stored token.
- New static asset `static/js/vendor/socket.io.min.js` (drop-in, matches the
  vendor pattern used for Vue).
- New Vue panel "CHAT" mounted only when `chatAvailable && hasWsToken`:
  - Left: conversation list. New ULID generated client-side for a fresh
    conversation; existing list sourced from `detail.sessions.sessions`
    filtered to `channel_name === "websocket"`.
  - Right: scrollable transcript + composer textarea. Inbound model
    messages appear via Socket.IO `message` events; outbound from the operator
    emits the same event shape pillbug-websocket clients use.
  - Token form: shown above the chat when `!hasWsToken`; once saved, chat
    panel renders. Clear-token button restores the form and disconnects the
    socket.
  - Connect call:

    ```js
    const socket = io(channelConnectUrl, {
      transports: ['polling'],
      extraHeaders: {
        Authorization: `Bearer ${wsToken}`,
        'X-SessionID': conversationUlid,
      },
    });
    ```

  - Connection lifecycle: open on panel mount (after token + conversation
    chosen), close on unmount or runtime switch. Show a small banner
    (`CONNECTING`, `LIVE`, `RECONNECTING`, `AUTH FAILED — UPDATE TOKEN`) tied
    to the socket state. On `connect_error` with an auth code, surface the
    "update token" prompt rather than auto-retrying.
  - Reuse `formatTimestamp` and the dashboard's existing typographic styles in
    [app.css](packages/pillbug-dashboard/src/pillbug_dashboard/static/css/app.css).

### Quick Send rework

- Keep the existing send form, but move it under a collapsed `QUICK SEND`
  panel header. Default-collapsed when chat is available; default-open when
  chat is not.
- Remove the channel `<select>` default-seeding logic that prefers non-a2a —
  the chat panel now handles the common path, so quick-send should default to
  empty and force an explicit choice.

### Tests

- Dashboard unit: `chatAvailable` computed gating.
- Manual: end-to-end smoke against a runtime started with
  `uv sync --extra websocket` (or whatever the package's extra is named —
  verify in `pyproject.toml`) confirming round-trip messages and reconnect
  after a forced disconnect.

### Risk / non-goals

- No attempt to render attachments, threading, or multi-operator collaboration
  in v1. Operator sees text only; attachments stay on the `inbox/` filesystem
  path the websocket channel already uses.
- No persistent transcript storage in the dashboard — the runtime is the
  source of truth. Chat panel always rehydrates from the runtime's session
  history (see §4) on open.

---

## 3. Edit Scheduled Tasks

Goal: full CRUD on `AgentTaskDefinition` from the dashboard, not just
enable/disable/run-now.

### Runtime changes

`manage_agent_task` already supports `create / update / delete` as an MCP tool
([app/mcp.py:2309](app/mcp.py#L2309)) but it is bound to model-driven flows
and goes through the planning gate. Add three thin operator-facing control
endpoints that delegate to the same store
([app/runtime/scheduler.py](app/runtime/scheduler.py)) but bypass the
planning gate (operator authority, not model authority — same model as the
existing approve/commit endpoints):

- `POST /control/tasks` — body: full `AgentTaskDefinition` (validated by the
  existing Pydantic model from
  [app/schema/tasks.py](app/schema/tasks.py)). Returns the created record.
- `PATCH /control/tasks/{task_id}` — body: partial update. Reuse the same
  validation `manage_agent_task` action="update" applies.
- `DELETE /control/tasks/{task_id}` — returns the deleted record id.

All three audit via `_audit_control_action` for parity with the existing
control surface.

### Dashboard changes

- `routes/api.py`: three new proxies — `POST /api/runtimes/{rid}/control/tasks`,
  `PATCH /api/runtimes/{rid}/control/tasks/{task_id}`,
  `DELETE /api/runtimes/{rid}/control/tasks/{task_id}`.
- `schema.py`: add a `TaskUpsert` model that mirrors `AgentTaskDefinition`
  field-for-field (so dashboard validation matches runtime validation before
  the request goes out).
- Task panel in `runtime_detail.html`:
  - Add a `NEW TASK` button beside the panel header.
  - Each row gets an additional `EDIT` and `DEL` button.
  - Clicking opens a modal with a form covering:
    `name`, `description`, `schedule_kind` (cron|delayed),
    `schedule_detail` (cron expression OR ISO delay), `enabled`, `goal.*`
    fields (`prompt`, `channel`, `conversation_id`, optional
    `max_cost_per_run_usd`). Use a tabbed or accordion layout to avoid a wall
    of fields.
  - Validation client-side: required fields, cron expression sanity (regex
    matching the same form the runtime accepts), at-least-one-goal-field rule.
  - On submit: POST/PATCH/DELETE through the new proxies, refresh detail on
    success, surface backend validation errors verbatim into the form.
- Confirm dialog used for `DEL` (reuses existing `PillbugDashboardConfirm`).

### Tests

- Backend: integration tests for create/update/delete endpoints covering
  validation errors, missing-bearer 401, unknown-task-id 404.
- Dashboard: unit tests for the new proxy routes; smoke test for the modal
  open/close + submit happy path (using a lightweight DOM harness if one is
  already in the repo, otherwise document as manual).

### Open question

- Should the dashboard also expose **scheduler-wide controls** (pause all
  enabled tasks, clear run history)? Out of scope for v1 unless someone asks;
  list it as a follow-up.

---

## 4. Tracked Conversation Preview

Goal: clicking a row in `TRACKED CONVERSATIONS` opens a read-only panel
showing the conversation's message history (model + user turns).

### Runtime changes

Add one telemetry endpoint:

- `GET /telemetry/sessions/{session_key}/history?limit=200` — returns the
  GeminiChatSession's history list as serialized turns
  `[{ role, content_text, occurred_at }, ...]`. Source: the per-session
  `GeminiChatSession` already kept on `ApplicationLoop`
  ([app/runtime/loop.py](app/runtime/loop.py)). Cap default to 200 turns
  (configurable via `PB_SESSION_HISTORY_PREVIEW_LIMIT` to stay consistent with
  the existing `PB_*` settings convention in
  [app/core/config.py](app/core/config.py)).
- Redaction: pass each turn through the same security pattern check used for
  inbound messages before serializing, so secrets stored in
  `{RUNTIME_BASE_DIR}/security_patterns.json` are masked in the preview.

### Dashboard changes

- `runtime_client.get_session_history(base_url, token, session_key, limit)`.
- `routes/api.py`: `GET /api/runtimes/{rid}/sessions/{session_key}/history`
  (read-only; uses telemetry token path, not control).
- In `runtime_detail.html`: add a `VIEW` button to each row in the sessions
  table beside `CLR`.
  - Click opens a side drawer or modal: header shows session_key + channel +
    message count + last activity; body is a virtualised list of turns with
    role-tinted backgrounds (user vs model vs system) and a `Copy` action per
    turn.
  - Drawer fetches lazily on open; renders a skeleton while loading; surfaces
    fetch errors inline.
  - No auto-refresh: history is a snapshot. Add a small `REFRESH` button in
    the drawer header for explicit refetch.
- Close button + ESC key dismiss the drawer.

### Tests

- Backend integration: history endpoint returns redacted turns; 404 on unknown
  session; 401 without bearer.
- Dashboard: proxy route test; manual UI smoke for opening / closing /
  switching between sessions.

### Open question

- Does the runtime store turns in a stable, ordered form across all channels
  (websocket vs cli vs a2a)? Verify in `GeminiChatSession`. If a2a / scheduled
  task sessions use a different shape, normalize to the common turn schema in
  the new endpoint rather than in the dashboard.

---

## Sequencing

1. **Section 1 (Drafts)** — smallest backend addition, biggest UX impact.
   Ship first; the new confirm-with-comment dialog component is reused by §3.
2. **Section 3 (Task CRUD)** — independent of §1; can go in parallel by a
   second author. Backend control endpoints first, then dashboard.
3. **Section 4 (Conversation preview)** — depends on history endpoint design;
   should land before §2 so the chat panel can rehydrate transcripts from the
   same endpoint.
4. **Section 2 (Chat)** — largest piece; bundles Socket.IO vendor asset, new
   ticket-auth flow, and a substantial Vue panel. Builds on the drawer pattern
   from §4 for the chat history rehydration view.

## Cross-cutting

- All new control endpoints must (a) require the dashboard bearer via
  `_authorize_control`, (b) audit through `_audit_control_action`, (c) live
  alongside the existing operator-narrow surface — no generic remote
  execution.
- All new telemetry endpoints must use `_authorize_telemetry` and stay
  read-only.
- Static assets (Socket.IO) follow the `static/js/vendor/` drop-in pattern
  already established for Vue.
- Update [packages/pillbug-dashboard/README.md](packages/pillbug-dashboard/README.md)
  "UI surface" and "Current scope" lists after each section ships.
- Add memory notes (`Status:` / `Architecture:`) at the end of each shipped
  section so future audits see the new endpoints and dashboard panels.
