# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.4] - 2026-05-28

- **Fixed** model turns no longer crash when the model calls a tool that doesn't exist (e.g. a hallucinated tool from a local model behind `pillbug-genai-proxy`); the runtime re-prompts the model with the missing tool name and the list of available tools so it can answer or pick a real tool instead of failing silently

## [0.1.3] - 2026-05-26

- **Added** operator dashboard drafts panel — new `GET /telemetry/drafts` runtime endpoint lists pending outbound drafts and command approvals; COMMIT/DISCARD and APPROVE/DENY actions, inline message/argv preview, expandable full-content rows
- **Added** operator dashboard chat panel — Socket.IO-backed conversation view on runtimes with the `websocket` channel; operator token stored in `localStorage` per runtime; conversation picker, transcript rehydration, connection status banner; HTTP-polling transport for custom header support in browser
- **Added** operator dashboard task CRUD — `POST /control/tasks`, `PATCH /control/tasks/{id}`, `DELETE /control/tasks/{id}` runtime endpoints; task modal with all `AgentTaskDefinition` fields, cron/delayed schedule picker, and client-side validation
- **Added** operator dashboard conversation preview — `GET /telemetry/sessions/{session_key}/history` runtime endpoint with security-pattern redaction; VIEW button per session opens transcript drawer with role-tinted turns, lazy load, and refresh
- **Changed** dashboard runtime detail page to single-column layout; panel order: OPERATOR → CHAT → DRAFTS → SESSIONS → TASKS → PEERS → EVENTS
- **Changed** Quick Send moved under collapsible section in OPERATOR panel; default-collapsed when websocket chat is available; `PillbugDashboardConfirm` extended with optional comment input
- **Fixed** websocket conversation list ordering
- **Fixed** stale CSS error selector

## [0.1.2]

- Less verbosity about DANGEROUSLY_APPROVE_EVERYTHING
- Make temperature and top_p None by default
- Bundled skills now support secrets
- Update documentation

## [0.1.1]

- Harness hardening

## [0.1.0]

### Added

- Initial implementation
