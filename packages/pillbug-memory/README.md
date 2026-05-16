# pillbug-memory

Optional bundled memory store for Pillbug, exposed as MCP tools. Memories are
flat Markdown files with YAML-style frontmatter, kept under the workspace at
`workspace/memory/` so the existing workspace sandbox applies. No database, no
embeddings, no subprocess — `pathlib` + `aiofile` only.

## Install

```bash
uv sync --extra memory
```

After installing the extra, register the plugin in `PB_MCP_TOOL_FACTORIES`:

```bash
export PB_MCP_TOOL_FACTORIES=memory=pillbug_memory:register_memory_tools
```

The runtime then exposes five MCP tools against the local composition server at
startup: `memory_list`, `memory_get`, `memory_add`, `memory_update`,
`memory_delete`. No channel plugin or other wiring is required.

## Configuration

| Variable                | Default   | Notes                                                                                   |
| ----------------------- | --------- | --------------------------------------------------------------------------------------- |
| `PB_MCP_TOOL_FACTORIES` | _(empty)_ | Add `memory=pillbug_memory:register_memory_tools` (comma-separated with other plugins). |
| `PB_MEMORY_DIR`         | `memory`  | Workspace-relative path. Absolute or `..` paths are rejected.                           |

The directory is created lazily on first write.

## File layout

```text
workspace/memory/
  MEMORY.md            # ledger maintained by the package; safe to hand-edit
  <id>.md              # one memory per file, frontmatter + body
```

Each memory file looks like:

```markdown
---
id: 0bdfae8e3a3b4f0a9f0e2b6f9d4a1c7e
name: "user is a senior backend engineer"
description: "they prefer terse responses and skip the obvious"
type: user
tags: ["preferences"]
created: 2026-05-13T14:23:00Z
updated: 2026-05-13T14:23:00Z
---

Free-form markdown body.
```

The package treats the body as opaque text. The on-disk fields are validated
against a Pydantic model on read; corrupt files are skipped during `memory_list`
and `repair_index` rather than crashing the runtime.

## Tools

- `memory_list(query?, type?, tag?, limit=50)` — substring match on
  name/description (case-insensitive), filters by `type` and exact tag
  membership. Returns frontmatter summaries only.
- `memory_get(id)` — full record (frontmatter + body). `id` may be a unique
  prefix.
- `memory_add(name, description, body, type, tags?)` — creates a new file and
  appends to `MEMORY.md`.
- `memory_update(id, ...)` — partial update of frontmatter and/or body; bumps
  `updated` and rewrites the index.
- `memory_delete(id)` — removes the file and its index line.

All paths route through the workspace sandbox in
[`app/util/workspace.py`](../../app/util/workspace.py); nothing outside the
configured memory directory is reachable.

## Non-goals

- Semantic search / embeddings (use external memory MCPs for that tier).
- Graph relationships between memories.
- Cross-runtime synchronization.
- Automatic eviction or TTL.
