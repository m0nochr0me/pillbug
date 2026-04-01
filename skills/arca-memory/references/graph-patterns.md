# Graph Patterns for Arca Memory

## Contents

1. [Relationship Type Vocabulary](#1-relationship-type-vocabulary)
2. [Edge Directionality and Backend Mechanics](#2-edge-directionality-and-backend-mechanics)
3. [Core Patterns](#3-core-patterns)
4. [Worked Examples](#4-worked-examples)
5. [Anti-Patterns](#5-anti-patterns)

---

## 1. Relationship Type Vocabulary

Use lowercase with underscores. Prefer these canonical labels over ad hoc strings to enable filtered traversal.

### Structural

| Label | Meaning |
|-------|---------|
| `depends_on` | Source cannot proceed without target |
| `blocks` | Source prevents target from starting |
| `requires` | Source needs target as a precondition |
| `part_of` | Source is a component of target |
| `contains` | Source is a container of target |
| `precedes` | Source comes before target in a sequence |

### Semantic

| Label | Meaning |
|-------|---------|
| `related_to` | Loose topical association — use as last resort |
| `contradicts` | Source conflicts with or invalidates target |
| `supports` | Source provides evidence or rationale for target |
| `supersedes` | Source replaces target (target is stale) |
| `derived_from` | Source was derived from or inspired by target |

### Project / Workflow

| Label | Meaning |
|-------|---------|
| `drove` | Source (constraint/goal) drove target (decision) |
| `driven_by` | Source (decision) was driven by target (constraint/goal) |
| `implements` | Source implements target design |
| `tracks` | Source is a progress update for target |
| `version_of` | Source is a newer version of target |

---

## 2. Edge Directionality and Backend Mechanics

Edges are **directed** and stored only on the **source node**. No automatic back-edges are created.

```
memory_connect(source_id="A", target_id="B", relationship_type="depends_on")
# Creates: A → depends_on → B
# Does NOT create: B → depended_on_by → A
```

To create a bidirectional relationship, call `connect` twice:

```
memory_connect(source_id="A", target_id="B", relationship_type="related_to")
memory_connect(source_id="B", target_id="A", relationship_type="related_to")
```

`memory_traverse` follows outgoing edges BFS from the starting node. Results include `_depth` (hop count). Filter by `relationship_type` to traverse only a specific edge kind.

### Idempotency

Calling `connect` with the same `(source, target, relationship_type)` triplet is a no-op — the backend detects the duplicate and skips. However, two edges between the same nodes with **different** labels are both stored:

```
memory_connect(A, B, "related_to")   # stored
memory_connect(A, B, "depends_on")   # also stored — different label
```

### Efficiency: Connect at Insert Time

`memory_add` accepts `connected_nodes` and `relationship_types` at creation — no separate `connect` call needed when you already know the UUIDs:

```
decision_id = memory_add(
    "Decision: use Aurora PostgreSQL.",
    bucket="decisions",
    connected_nodes=[constraint_id, goal_id],
    relationship_types=["driven_by", "driven_by"]
)["memory_id"]
```

---

## 3. Core Patterns

### Pattern 1: Decision + Rationale Cluster

Link a decision to the constraints and goals that drove it. Store edges on the decision node so you can traverse outward to find rationale.

```
constraint_id = memory_add(
    "Constraint: system must support 10k concurrent writes/second.",
    bucket="project-backend"
)["memory_id"]

goal_id = memory_add(
    "Goal: minimize operational complexity — prefer managed services.",
    bucket="project-backend"
)["memory_id"]

decision_id = memory_add(
    "Decision: use CockroachDB for production database.",
    bucket="decisions",
    connected_nodes=[constraint_id, goal_id],
    relationship_types=["driven_by", "driven_by"]
)["memory_id"]

# Later: traverse from the decision to find what drove it
memory_traverse(decision_id, relationship_type="driven_by", depth=1)
```

### Pattern 2: Task Dependency Chain

Model sequential tasks where order matters.

```
task_a = memory_add("Task: implement JWT auth endpoint.", bucket="project-x")["memory_id"]
task_b = memory_add("Task: implement user profile API.", bucket="project-x")["memory_id"]
task_c = memory_add("Task: implement admin dashboard.", bucket="project-x")["memory_id"]

memory_connect(task_b, task_a, "depends_on")  # profile depends on auth
memory_connect(task_c, task_b, "depends_on")  # dashboard depends on profile

# Find all blockers for task C (returns task_b at depth=1, task_a at depth=2)
memory_traverse(task_c, relationship_type="depends_on", depth=3)
```

### Pattern 3: Supersession (Versioning)

When a decision changes, preserve history — mark old fact stale with `supersedes`.

```
old_results = memory_get("database decision", bucket="decisions")
old_id = old_results["results"][0]["memory_id"]

new_id = memory_add(
    "Decision: migrate from CockroachDB to Aurora PostgreSQL (2026-02).",
    bucket="decisions",
    connected_nodes=[old_id],
    relationship_types=["supersedes"]
)["memory_id"]
```

Do NOT delete the old decision — keep it for historical context. When querying, the node without an incoming `supersedes` edge is current. The one with an incoming `supersedes` edge is stale.

### Pattern 4: Topic Hub

Create a hub node that clusters related memories under a theme for bulk retrieval.

```
hub_id = memory_add(
    "Topic: API design standards for the platform.",
    bucket="decisions"
)["memory_id"]

memory_connect(hub_id, rest_style_id, "contains")
memory_connect(hub_id, versioning_id, "contains")
memory_connect(hub_id, auth_scheme_id, "contains")

# Retrieve all specifics under the hub
memory_traverse(hub_id, relationship_type="contains", depth=1)
```

### Pattern 5: Progress Tracking

Attach status updates to a goal with `tracks` edges.

```
goal_id = memory_add(
    "Goal: ship v2.0 by 2026-03-15.",
    bucket="project-x"
)["memory_id"]

# At session end, store progress
status_id = memory_add(
    "Status: auth and core API complete; UI 40% done (2026-02-20).",
    bucket="project-x",
    connected_nodes=[goal_id],
    relationship_types=["tracks"]
)["memory_id"]

# Find all status updates for the goal (traverse from goal if bidirectional needed)
# Or: memory_get("status update for v2.0", bucket="project-x")
```

---

## 4. Worked Examples

### Example A: Linking a new decision to existing context

Scenario: user decides to adopt a monorepo structure, replacing a multi-repo approach.

```
# 1. Find existing relevant memories
results = memory_get("repository structure, code organization strategy")
old_id = results["results"][0]["memory_id"]  # "codebase was originally multi-repo"

# 2. Store the new decision, linking immediately
new_id = memory_add(
    "Decision: adopt monorepo structure using Turborepo (2026-02).",
    bucket="decisions",
    connected_nodes=[old_id],
    relationship_types=["supersedes"]
)["memory_id"]
```

### Example B: Resuming project context after a gap

Scenario: resuming work on "project-backend" after time away.

```
# 1. Get broad context
results = memory_get(
    "What are the current goals, architecture decisions, and constraints?",
    bucket="project-backend",
    top_k=10
)

# 2. For key decision nodes, traverse to see their rationale
for result in results["results"]:
    mem_id = result["memory_id"]
    if result.get("connected_nodes"):
        rationale = memory_traverse(mem_id, depth=1)
        # rationale["results"] contains the driving constraints/goals
```

### Example C: Building a dependency map at project start

Scenario: user describes a new project's tasks; model the dependency graph.

```
design_id  = memory_add("Task: system design and ADRs.", bucket="project-new")["memory_id"]
infra_id   = memory_add("Task: provision infrastructure.", bucket="project-new")["memory_id"]
backend_id = memory_add("Task: implement backend API.", bucket="project-new")["memory_id"]
frontend_id= memory_add("Task: implement frontend.", bucket="project-new")["memory_id"]

memory_connect(infra_id,    design_id,  "depends_on")
memory_connect(backend_id,  infra_id,   "depends_on")
memory_connect(frontend_id, backend_id, "depends_on")

# Find the full critical path from frontend
memory_traverse(frontend_id, relationship_type="depends_on", depth=5)
# → Returns infra (depth=2), design (depth=3) automatically via BFS
```

---

## 5. Anti-Patterns

### Using `related_to` for everything

`related_to` is meaningless to filter in traversals and degrades the graph into an unstructured blob. Only use it when no more specific label applies.

### Storing UUIDs in content strings

Do not embed UUID strings inside the `content` field to simulate relationships. Use `connect` — it is queryable and traversable by the backend.

```
# Bad
memory_add("Decision: use PostgreSQL. Related to: a1b2c3d4-...", ...)

# Good
memory_add("Decision: use PostgreSQL.", ...)
memory_connect(decision_id, constraint_id, "driven_by")
```

### Connecting memories without having UUIDs

`connect` requires UUIDs. If you do not yet have the IDs, retrieve the memories first:

```
# Wrong order
memory_connect("some-content", "other-content", "related_to")  # fails — not UUIDs

# Right order
r1 = memory_get("some content query")
r2 = memory_get("other content query")
memory_connect(r1["results"][0]["memory_id"], r2["results"][0]["memory_id"], "related_to")
```

### Deep traversal on sparse graphs

`memory_traverse(id, depth=5)` on a sparsely connected graph generates many DB round-trips with little return. Keep depth at 1–2 unless you have a well-connected graph and a confirmed need.

### Forgetting bidirectionality

`traverse` only follows outgoing edges. If you need to answer both "what does X depend on" AND "what depends on X", create edges in both directions. Or design your graph so the traversal direction you need is the one with the edges.
