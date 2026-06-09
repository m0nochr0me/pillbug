# Pillbug Optimization Plan

Audit date: 2026-06-10 · Baseline: `master` @ `7d8d329` · 415 tests passing (3 skipped) in ~4s · `ruff check` clean

This document records the findings of a whole-repo audit (architecture, simplification,
maintainability) and a phased plan to act on them. Each phase is independently shippable and
ends with the same verification gate: `uv run pytest` green, `uv run ruff check .` clean, and
no change to public behavior (tool names, HTTP routes, `PB_` env contract, import paths used
by tests and packages).

---

## Summary of findings

| #   | Finding                                                                                                                                     | Severity | Effort  |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------- | -------- | ------- |
| 1   | `app/mcp.py` is a 3,600-line god module (22 MCP tools + 27 HTTP routes + dispatch + auth + server bootstrap)                                | High     | Medium  |
| 2   | Reverse dependency: `loop.py` locally imports private `_dispatch_outbound_draft` from `app.mcp` to dodge a circular import                  | High     | Low     |
| 3   | `ApplicationLoop` (1,575 lines) mixes channel consumption, command handling, draft-approval UX, response routing, and telemetry bookkeeping | Medium   | Medium  |
| 4   | Channel packages (telegram/matrix/slack/websocket) each re-implement ~8–10 identical helpers                                                | Medium   | Low     |
| 5   | `_utcnow()` is defined 13 times across `app/` and `packages/`                                                                               | Low      | Trivial |
| 6   | `.coverage` (binary test artifact) is tracked in git                                                                                        | Low      | Trivial |
| 7   | `app/scratch/` lives inside the shipped `app` package directory                                                                             | Low      | Trivial |
| 8   | `app/core/ai.py` (1,090 lines) and `app/runtime/scheduler.py` (1,357 lines) each hold several separable concerns                            | Medium   | Medium  |
| 9   | Ruff `C901` (complexity) is globally ignored                                                                                                | Low      | Low     |
| 10  | Docs split between root `doc/` and (new) `app/doc/`; `skills/dreaming` and `skills/nrem` are untracked                                      | Low      | Trivial |

What is deliberately **not** on the list:

- **Module-level singletons** (`settings`, `runtime_telemetry`, `approval_store`,
  `outbound_draft_store`, `task_scheduler`). They are a known testability tradeoff, but the
  suite currently covers them well and runs in 4 seconds. Replacing them with an app-context
  object would be a large, churn-heavy refactor with little observable payoff. Revisit only if
  test isolation starts hurting.
- **`Settings` as one flat class** (~37 `PB_` vars). Splitting into nested pydantic-settings
  groups would change env var names — a breaking contract change. The flat class is the
  documented single source of truth; keep it.
- **`fakeredis` as a production dependency.** It is the deliberate in-memory fallback in
  `app/core/redis.py` when Redis is not configured ("Redis is optional" is a stated
  constraint). Leave it; optionally add a code comment stating this is intentional.

---

## Phase 1 — Hygiene (trivial, zero risk)

1. **Untrack `.coverage`** and ignore it:
   `git rm --cached .coverage` and add `.coverage*` to `.gitignore`.
2. **Move `app/scratch/` out of the package tree** to the existing root `scratch/`
   (already gitignored). The build backend packages the whole `app` module
   (`module-root = ""`), so stray files under `app/` can leak into wheels built from a
   working tree.
3. **Decide on `skills/dreaming/` and `skills/nrem/`** — commit them or move to `scratch/`.
   Untracked directories that linger in `git status` hide real changes.
4. Verify: `git status` clean except intended changes; `uv build` output contains no scratch
   files.

## Phase 2 — Kill the cross-layer import (small, high leverage)

**Problem.** Outbound draft dispatch (`_dispatch_outbound_draft` and its four `_dispatch_send_*`
helpers, plus `_outbound_limits_for_channel` / `_check_outbound_budget` and
`_requires_approval_envelope`) lives in `app/mcp.py`, but the runtime loop needs it for the
`/yes` command and imports it locally ("local import: avoids reverse module import",
`app/runtime/loop.py:617`). The MCP layer should depend on the runtime, never the reverse.

**Plan.**

1. Create `app/runtime/outbound_dispatch.py` and move the dispatch helpers there verbatim.
   They already depend only on runtime-layer modules (`approvals`, `channels`,
   `outbound_budget`, telemetry, schema) — nothing MCP-specific.
2. `app/mcp.py` imports from the new module; delete the local import in `loop.py` and import
   normally at the top.
3. Verify: `tests/integration/test_outbound_drafts.py`, `test_runtime_commands.py`,
   `test_outbound_send_budget.py` pass unchanged; `grep -rn "from app.mcp import" app/runtime/`
   returns nothing.

## Phase 3 — Shared channel-plugin helpers (removes ~400 duplicated lines)

**Problem.** `pillbug-telegram`, `pillbug-matrix`, `pillbug-slack` (and partially
`pillbug-websocket`) each define near-identical copies of: `_split_csv`, `_chunk_message`,
`_sanitize_filename`, `_resolve_attachment_path`, `_render_attachment_text`,
`_parse_conversation_id` / `_build_conversation_id`, `_is_transient_*_error`,
`_log_*_failure`, and the download/send-attachment scaffolding. Every bug fix must be applied
three or four times (the threading/conversation-id pattern is already drifting between Matrix
and Slack).

**Plan.**

1. Add `app/runtime/channel_helpers.py` (channel packages already import from
   `app.runtime.channels`, `app.util.workspace`, etc., so no new dependency edge is created —
   and no new workspace package is needed).
2. Move the genuinely identical helpers there, parameterized where the copies differ only by a
   constant (`_chunk_message(max_chars=...)`, transient-error status-code sets, conversation-id
   separator). Keep channel-specific logic (Telegram file-type dispatch, Matrix msgtype
   resolution) in the plugins.
3. Migrate one package per commit: telegram → matrix → slack → websocket. Each commit deletes
   the local copies it replaces and must keep `tests/unit/packages/` green.
4. Verify: per-package unit tests pass; `grep -rn "def _chunk_message" packages/` returns
   nothing; behavior parity spot-checked via existing threading tests
   (`test_matrix_threading.py` etc.).

Include in this phase the trivial dedup: add `utcnow()` to `app/util/text.py`'s sibling
(`app/util/clock.py`, new, ~5 lines) and replace the 13 private `_utcnow` definitions across
`app/` and `packages/pillbug-dashboard`. Schema modules may keep a local alias if needed for
Pydantic `default_factory` import-cycle safety — check each call site rather than forcing it.

## Phase 4 — Split `app/mcp.py` into a package

**Problem.** One module owns: workspace file tools, outbound send/draft tools, command
execution + approval tools, planning-mode gate, todo tools, scheduled-task tool, `fetch_url`,
A2A peer discovery, 9 telemetry routes, 16 control routes, A2A inbound route, agent-card
routes, URL-shortener redirect, auth helpers, and uvicorn bootstrap. At 3,600 lines it is the
single biggest review/merge bottleneck, and module-level state (`_peer_card_cache`) hides in
the middle of it.

**Plan.** Convert to a package with `app/mcp/__init__.py` re-exporting the existing public
surface (`mcp`, `mcp_app`, `create_mcp_server`, `serve_mcp_server`, `bind_application_loop`)
so every existing import (`from app.mcp import ...`, `python -m app.mcp`) keeps working:

```
app/mcp/
├── __init__.py      # re-exports; keeps `from app.mcp import mcp, mcp_app, ...` working
├── __main__.py      # preserves `uv run python -m app.mcp`
├── server.py        # FastMCP/FastAPI instances, middleware, create/serve/bind
├── auth.py          # _extract_bearer_token, _authorize_{telemetry,control,a2a}, audit
├── shared.py        # _resolve_workspace_path, validators, envelope helpers
├── tools/
│   ├── files.py     # list/read/write/replace/search/find
│   ├── outbound.py  # send_*, draft/commit outbound, a2a peers
│   ├── commands.py  # execute/draft/run_approved command + env allowlist helpers
│   ├── planning.py  # enter/exit planning mode, gate, artifacts
│   ├── todo.py      # manage_todo_list + snapshot sync
│   ├── tasks.py     # manage_agent_task + goal builder
│   └── fetch.py     # fetch_url
└── http/
    ├── telemetry.py # /health, /telemetry/*
    ├── control.py   # /control/*
    ├── a2a.py       # /a2a/messages, agent-card routes
    └── shortener.py # /u/{token}
```

Rules for the split:

- **Pure code motion** — no signature, route, or tool-name changes. Tool registration order
  may change; nothing in the runtime depends on it, but confirm via
  `test_session_mcp_client.py`.
- The planning gate (`_enforce_planning_gate`) and `_PLANNING_READ_ONLY_TOOLS` move to
  `tools/planning.py` and are imported by the tool modules that consult them — the gate
  contract in CLAUDE.md is unchanged.
- `_peer_card_cache` becomes an explicit module attribute of `tools/outbound.py` with a short
  comment (it is process-local cache state, currently easy to miss).
- Do it in 3–4 commits (http routes first, then tools, then shared/auth) so each diff is
  reviewable and bisectable.

Verify: full suite green after each commit; `uv run python -m app.mcp` still serves;
`grep -rn "from app.mcp import"` call sites unchanged.

## Phase 5 — Slim `ApplicationLoop`

**Problem.** `loop.py` (1,575 lines) contains three separable responsibilities beyond its core
orchestration job:

- **Runtime command handling** (~430 lines): `/clear`, `/usage`, `/summarize`, `/yes`, `/no`,
  `/drafts` plus their rendering helpers (`_render_draft_line`, `_format_draft_line_with_age`,
  `_truncate_for_channel_reply`).
- **Session telemetry bookkeeping** (~250 lines): `_SessionTelemetryState` plus the nine
  `_record_*` methods and cache-ratio warning logic.
- **Core loop**: consume → debounce → pipeline → session → respond (this stays).

**Plan.**

1. Extract `app/runtime/commands.py` with a small command-handler class that receives the loop
   (or a narrow protocol of the loop's send/record methods). `_recognized_command` and the
   per-command handlers move; the loop keeps a one-line delegation in `_flush_messages`.
2. Extract `app/runtime/session_telemetry.py` with `_SessionTelemetryState` and the
   `_record_*` family as methods on a `SessionTelemetryTracker` owned by the loop.
3. No behavior change: command set, reply texts, and telemetry event shapes stay identical —
   `test_runtime_commands.py`, `test_cache_telemetry.py`, and
   `test_session_history_telemetry.py` are the regression gate.

## Phase 6 — Optional follow-ups (do only when touching these areas anyway)

- **`app/core/ai.py` → package** (`service.py` for `GeminiChatService`, `session.py` for
  `GeminiChatSession`, `attachments.py` for the MIME/attachment helpers). Same
  re-export-from-`__init__` technique as Phase 4.
- **`app/runtime/scheduler.py`**: split task persistence (`_load_store`/`_persist_locked`/
  snapshots, ~120 lines) into a small `TaskStore` class; keeps the Docket worker/watchdog
  logic readable. Preserve the JSON store shape at `{RUNTIME_BASE_DIR}/tasks/agent_tasks.json`.
- **Re-enable Ruff `C901`** with a generous threshold (e.g. `max-complexity = 15`) once
  Phases 4–5 land, so the god-module pattern cannot quietly regrow. Address or `noqa` the
  residual hits explicitly.
- **Telemetry helper**: 59 `record_event(...)` call sites repeat the same envelope; a couple
  of thin domain wrappers (`record_session_event`, `record_control_event`) would trim
  boilerplate. Cosmetic — bundle with other work.
- **Docs consolidation**: root `doc/` holds INSTALL/CONFIGURATION; decide whether `app/doc/`
  (this file's location) or root `doc/` is canonical and move so there is one docs tree. Note
  that anything under `app/` ships in the wheel.

---

## Sequencing and risk

```text
Phase 1 (hygiene)            → verify: git status clean, wheel contents
Phase 2 (dispatch move)      → verify: outbound/draft/budget integration tests
Phase 3 (channel helpers)    → verify: per-package unit tests, one package per commit
Phase 4 (mcp package split)  → verify: full suite + `python -m app.mcp` after each commit
Phase 5 (loop extraction)    → verify: runtime command + telemetry integration tests
Phase 6 (opportunistic)      → no schedule; piggyback on feature work
```

Phases 1–3 are low-risk and high-leverage; do them first and in order (Phase 2 removes the
import knot that Phase 4 would otherwise have to work around). Phases 4–5 are pure code
motion guarded by an already-fast test suite. Nothing in this plan changes the `PB_` env
contract, MCP tool names, HTTP routes, channel plugin contract, or persisted file formats.
