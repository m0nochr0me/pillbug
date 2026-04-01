---
name: arca-memory
description: "Complete guide for using the Arca MCP persistent memory system. Use this skill when an agent needs to store, retrieve, update, or delete memories across sessions; when organizing information into buckets; when performing semantic search over stored facts; when building or traversing knowledge-graph relationships between memory nodes; or when implementing session lifecycle memory hygiene (session start recall, session end capture). Triggers on any request involving persistent memory, memory search, memory organization, or remembering/forgetting information using the mcp__arca_memory__* tools."
---

# Arca Memory

Arca provides persistent, semantically searchable memory across sessions, organized into
buckets within a namespace. Namespace isolation is pre-configured via the MCP connection
header — no agent action required.

## Tool Reference

| Tool | Signature |
|------|-----------|
| `memory_add` | `(content, bucket?, connected_nodes?, relationship_types?)` → `{memory_id}` |
| `memory_get` | `(query, bucket?, top_k=5)` → `{results}` |
| `memory_get_last` | `(n=5, bucket?)` → `{results}` — most recent by creation time |
| `memory_delete` | `(memory_id)` |
| `memory_clear` | `(bucket?)` — clears `"default"` if omitted |
| `memory_list_buckets` | `()` → `{buckets}` |
| `memory_connect` | `(source_id, target_id, relationship_type)` |
| `memory_disconnect` | `(source_id, target_id, relationship_type?)` |
| `memory_traverse` | `(memory_id, relationship_type?, depth=1)` |

For graph operations and detailed traversal patterns, read `references/graph-patterns.md`.

---

## Session Lifecycle

### Session Start

1. Call `memory_list_buckets` to orient — see what domains have history.
2. Call `memory_get` with a broad context query relevant to the current task before taking any action that may depend on prior decisions.
3. If the task is project-specific, query that project bucket explicitly.

```
# Example: starting a coding session
memory_get("project goals, decisions, and constraints", bucket="project-arca-mcp")
memory_get("user preferences and workflow style", bucket="preferences")
```

### During a Session

- Store facts immediately when they become known — do not batch to end-of-session.
- After storing a memory, capture the returned `memory_id` if you intend to link it to other nodes.
- Update stale facts by deleting the old memory and adding a new one (no in-place update exists).

### Session End

Before ending, store:
- Decisions made and their rationale
- Current project state ("Feature X is 60% complete — auth done, UI pending")
- Any unresolved items or blockers
- User preferences expressed during the session

---

## Storage Quality

### What to Store

Store stable, reusable facts that would otherwise be re-discovered:

- User preferences and working style
- Project goals, deadlines, architecture decisions
- Outcomes of completed tasks
- Recurring patterns or constraints
- Named entities (people, systems, tools) and their roles

### Content Phrasing Rules

Write content as short, declarative, present-tense statements:

| Good | Bad |
|------|-----|
| `"User prefers 4-space indentation in Python."` | `"The user said they like to use 4 spaces when writing Python."` |
| `"Project deadline is 2026-03-15."` | `"We talked about the deadline and it's March 15th next year."` |
| `"Auth uses JWT with RS256; tokens expire after 1 hour."` | `"JWT stuff"` |
| `"Decision: use PostgreSQL over SQLite for production load."` | `"They chose postgres because sqlite wasn't good enough."` |

**Prefix convention:**

- `"Decision: ..."` — architectural or process choices
- `"Constraint: ..."` — hard limits the agent must respect
- `"Goal: ..."` — objectives to work toward
- `"Status: ..."` — current state of work in progress

### What NOT to Store

- Transient data: current file contents, API responses, build output
- Redundant facts already in the codebase or documentation
- Opinions without context ("user seemed frustrated")
- Ephemeral session details that will not be relevant next session
- Raw code dumps — store design decisions instead

---

## Bucket Taxonomy

Call `memory_list_buckets` before creating new buckets to avoid duplicates.

### Recommended Buckets

| Bucket | Contents |
|--------|----------|
| `preferences` | User UI/UX preferences, communication style, tooling choices |
| `profile` | Stable personal facts: timezone, language, role, org |
| `decisions` | Cross-project architectural and process decisions |
| `project-{slug}` | Everything scoped to a specific project (goals, status, constraints) |
| `people` | Information about team members, stakeholders |
| `systems` | External services, APIs, infrastructure the user works with |
| `default` | Avoid — only use if no bucket applies |

### Naming Rules

- Lowercase with hyphens: `project-arca-mcp`, not `ProjectArcaMCP`
- Use `project-{slug}` not `{slug}-project`
- Keep slugs short but unambiguous: `project-ecomm` not `project-e`

---

## Retrieval Strategy

### Query Phrasing

The backend uses Gemini's `RETRIEVAL_QUERY` task type (asymmetric embedding). Phrase queries as questions or natural descriptions, not keywords:

| Better | Worse |
|--------|-------|
| `"What are the user's code style preferences?"` | `"code style"` |
| `"What decisions were made about the database schema?"` | `"database"` |
| `"What is the current status of the authentication feature?"` | `"auth status"` |

### Bucket Targeting

- Provide `bucket` when you know the domain — filters before vector search and improves precision.
- Omit `bucket` for cross-domain queries or when the bucket is unknown.

### top_k

| Scenario | Recommended `top_k` |
|----------|---------------------|
| Specific fact lookup | 3 |
| Context recall at session start | 10 |
| Broad domain scan | 20 |
| Full history review | 50 |

### Chronological Retrieval

Use `memory_get_last` when you need the most recently stored memories rather than the most semantically similar:

| Scenario | Recommended approach |
|----------|---------------------|
| "What did we just discuss?" | `memory_get_last(n=5)` |
| "Recent decisions in this project" | `memory_get_last(n=10, bucket="project-foo")` |
| "Find memories about auth" | `memory_get("authentication decisions")` (semantic) |

All memories now carry a `created_at` UTC timestamp. Pre-existing memories (created before the timestamp migration) have `created_at: null` and sort last in chronological queries.

### Handling No Results

If `memory_get` returns empty results:
1. Retry with a broader query (remove bucket filter, rephrase)
2. Call `memory_list_buckets` to verify the expected bucket exists
3. If still empty, the information has not been stored — do not hallucinate prior context

---

## Memory Hygiene

### When to Delete

- A fact has been explicitly superseded ("we switched from PostgreSQL to CockroachDB")
- A deadline or milestone has passed and is no longer relevant
- A project is complete and its bucket is no longer needed

### Updating a Fact

No update operation exists. To update:
1. `memory_get` to find the stale memory and capture its `memory_id`
2. `memory_delete(memory_id)` to remove it
3. `memory_add` with the corrected content

### Clearing a Bucket

`memory_clear(bucket=None)` clears the `"default"` bucket, **not all buckets**. Always pass `bucket` explicitly:

```
memory_clear(bucket="project-old-app")  # correct — clears named bucket
memory_clear()                           # clears "default" only — not a namespace wipe
```

Only clear a bucket when explicitly instructed by the user. This operation is irreversible.

---

## Knowledge Graph

Use graph edges to encode structural relationships between memories that semantic search alone cannot capture — hierarchies, dependencies, sequences, and clusters.

Read `references/graph-patterns.md` before performing any graph operations. It covers relationship type vocabulary, edge directionality mechanics, core patterns with worked examples, and anti-patterns.

### When to Use Graphs

- Linking a decision to the constraints that drove it
- Tracking task dependencies ("task B requires task A")
- Building topic clusters (related concepts linked under a hub node)
- Versioning: linking old and new versions of a decision via `supersedes`

### When NOT to Use Graphs

- When a single bucket query would suffice
- For one-off lookups with no structural meaning
- When you don't have the UUIDs yet (retrieve first, then connect)
