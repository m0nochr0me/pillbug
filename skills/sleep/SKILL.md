---
name: sleep
description: >
  Automated maintenance cycle modeled after human sleep stages (NREM and REM). Performs
  nightly knowledge acquisition from RSS feeds, memory consolidation (review, merge, prune,
  connect), self-reflection on recent actions and responses, and dream-like projective
  simulation via A2A when available. Triggers when the user asks to set up or manage the
  sleep cycle, run a sleep session manually, check sleep logs,
  or adjust bedtime reading categories. All scheduled task messages are sent to channel `cli`.
---

## Overview

The Sleep maintenance cycle is a scheduled routine that mirrors the restorative stages of
human sleep. It runs as a single scheduled task that executes four sequential phases:

| Phase | Analogy | Purpose |
|-------|---------|---------|
| **Bedtime Reading** | Pre-sleep reading | Acquire new knowledge from RSS feeds |
| **Memory Consolidation** | NREM deep sleep | Review, organize, prune, merge, and connect memories |
| **Self-Reflection** | NREM light sleep | Analyze recent actions, identify patterns, save insights |
| **Projection** | REM dreaming | Simulate fictional scenarios, test reactions, extract insights |

## Session Start

Before doing anything else:

1. Call `mcp__arca_memory__memory_list_buckets` to orient.
2. Call `mcp__arca_memory__memory_get` with query `"sleep cycle configuration and last run"` and `bucket="mia-worklog"`.
3. Route based on user intent (see Routing below).

## Routing

| User intent | Action |
|-------------|--------|
| Set up / configure sleep cycle | Read [references/onboarding.md](./references/onboarding.md) |
| Run sleep manually / sleep now | Execute full cycle (all 4 phases sequentially) |
| Check sleep logs / last sleep | Query `mia-worklog` for `"sleep cycle run log"` |
| Adjust bedtime reading category | Update memory in `mia-worklog` with new category preference |
| Scheduled task executing | Execute full cycle (all 4 phases sequentially) |
| Pause / resume sleep | Update task via `manage_agent_task(action="update", enabled=True/False)` |

## Phase 1: Bedtime Reading

**Goal:** Fetch a single article from a subscribed RSS feed category, extract useful knowledge,
and incorporate it into the agent's own capabilities.

### Steps

1. Retrieve the configured reading category from memory:
   ```
   mcp__arca_memory__memory_get(query="sleep bedtime reading category", bucket="mia-worklog")
   ```

2. List new posts from the configured category using `feed-reader`:
   ```bash
   {WORKSPACE_DIR}/skills/feed-reader/scripts/feed_reader.sh \
     --workspace {WORKSPACE_DIR} list CATEGORY
   ```

3. If posts are available, pick **one** article (prefer unread, rotate selection to avoid
   always picking the first). Fetch its content:
   ```bash
   {WORKSPACE_DIR}/skills/feed-reader/scripts/feed_reader.sh \
     --workspace {WORKSPACE_DIR} fetch NUMBER
   ```

4. Read the fetched article file from `{WORKSPACE_DIR}/fetched/`.

5. **Evaluate the article** — determine if it contains actionable knowledge:
   - New technical concept, tool, or methodology relevant to current work?
   - Pattern, convention, or best practice worth internalizing?
   - Information that updates or contradicts existing knowledge?

6. **Incorporate useful knowledge** through one or more of these actions:
   - **Develop a new skill**: If the article describes a complete workflow or capability,
     create a new skill directory under `{WORKSPACE_DIR}/skills/` with a valid `SKILL.md`.
   - **Add to personal memory**: Store concise, declarative facts in `mia-worklog` or
     a relevant bucket using appropriate prefixes (`Decision:`, `Architecture:`, `Constraint:`).
   - **Update AGENTS.md**: If the knowledge changes how the agent should behave or respond,
     append a guideline to `{WORKSPACE_DIR}/AGENTS.md`.
   - **Skip**: If the article is not useful, log that it was read but no action taken.

7. Store a reading log entry:
   ```
   mcp__arca_memory__memory_add(
     content="Sleep:Reading | {date} | {article_title} | Action: {action_taken_or_skipped}",
     bucket="mia-worklog"
   )
   ```

## Phase 2: Memory Consolidation (NREM Deep Sleep)

**Goal:** Review all memory buckets, remove obsolete entries, merge duplicates, simplify
verbose memories, and strengthen the knowledge graph with connections.

### Steps

1. List all buckets:
   ```
   mcp__arca_memory__memory_list_buckets()
   ```

2. For each bucket, retrieve recent memories:
   ```
   mcp__arca_memory__memory_get_last(n=20, bucket=BUCKET)
   ```

3. **Delete obsolete entries**: Remove memories that reference:
   - Completed tasks or resolved issues
   - Outdated status information superseded by newer entries
   - Temporary context no longer relevant
   ```
   mcp__arca_memory__memory_delete(memory_id=ID)
   ```

4. **Merge similar items**: When two or more memories express the same concept:
   - Create a single consolidated memory combining the information
   - Delete the originals
   - Preserve any connections from the originals on the new entry
   ```
   mcp__arca_memory__memory_add(content="merged content", bucket=BUCKET, connected_nodes=[...])
   mcp__arca_memory__memory_delete(memory_id=OLD_1)
   mcp__arca_memory__memory_delete(memory_id=OLD_2)
   ```

5. **Simplify verbose memories**: If a memory exceeds ~200 characters and can be expressed
   more concisely without losing meaning, replace it:
   - Add the simplified version
   - Delete the verbose original
   - Re-establish connections

6. **Create connections** between related memories across buckets:
   - Use `related_to` for topically related items
   - Use `supports` when one memory reinforces another
   - Use `contradicts` when memories conflict (flag for resolution)
   - Use `supersedes` when a newer memory replaces an older one
   - Use `part_of` for hierarchical relationships
   - Use `derived_from` for knowledge that originated from another memory
   ```
   mcp__arca_memory__memory_connect(source_id=A, target_id=B, relationship_type="related_to")
   ```

7. **Generalize patterns**: If 3+ memories describe similar specific cases, consider creating
   a generalized memory and connecting the specifics to it with `derived_from`.

8. Store a consolidation log:
   ```
   mcp__arca_memory__memory_add(
     content="Sleep:Consolidation | {date} | Deleted: {n} | Merged: {n} | Simplified: {n} | Connected: {n} | Generalized: {n}",
     bucket="mia-worklog"
   )
   ```

## Phase 3: Self-Reflection (NREM Light Sleep)

**Goal:** Analyze recent actions, responses, and decisions to identify what worked well
and what could be improved.

### Steps

1. Retrieve recent worklog entries to understand what happened since last sleep:
   ```
   mcp__arca_memory__memory_get_last(n=15, bucket="mia-worklog")
   ```

2. Also check client-facing interactions:
   ```
   mcp__arca_memory__memory_get(query="recent interaction outcomes and feedback", bucket="client")
   ```

3. **Analyze patterns** across recent activity:
   - Were there repeated mistakes or inefficiencies?
   - Were there interactions where the response quality was notably high or low?
   - Were there moments of uncertainty where better preparation would have helped?
   - Were there successful strategies worth reinforcing?

4. **Formulate insights** as actionable, concise observations:
   - Good: `"Reflection:Positive | Proactive context retrieval before complex tasks reduced back-and-forth"`
   - Improvement: `"Reflection:Improve | Tended to over-explain simple confirmations — keep acks shorter"`
   - Pattern: `"Reflection:Pattern | Client prefers receiving options before decisions are made"`

5. Store reflection insights (limit to 2-4 per cycle to avoid memory bloat):
   ```
   mcp__arca_memory__memory_add(
     content="Reflection:{type} | {date} | {insight}",
     bucket="mia-worklog"
   )
   ```

6. If a reflection directly relates to a client preference or communication pattern,
   also store it in the appropriate bucket:
   ```
   mcp__arca_memory__memory_add(
     content="Preference: {observed pattern}",
     bucket="client"
   )
   ```

7. Connect reflection insights to the memories that triggered them:
   ```
   mcp__arca_memory__memory_connect(source_id=REFLECTION_ID, target_id=TRIGGER_ID, relationship_type="derived_from")
   ```

## Phase 4: Projection (REM Dreaming)

**Goal:** Generate a fictional scenario, simulate a response, reflect on the simulation,
and extract transferable insights. This phase only runs when A2A connectivity is available.

### Steps

1. **Check A2A availability**: Determine if an A2A peer connection is configured and
   reachable. If not, skip this phase and log:
   ```
   mcp__arca_memory__memory_add(
     content="Sleep:Dream | {date} | Skipped — no A2A peer available",
     bucket="mia-worklog"
   )
   ```

2. **Generate dream seeds**: Select 3-5 keywords from recent memories, mixing:
   - One keyword from a recent task or project
   - One keyword from a recent reflection
   - One keyword from bedtime reading (if available)
   - One or two random thematic words for novelty

3. **Request dream generation**: Send the keywords to an A2A peer with a request to
   generate a short fictional scenario (2-4 sentences) incorporating those keywords:
   ```
   send_message(
     channel="a2a:{peer_runtime_id}",
     message="Generate a short fictional scenario (2-4 sentences) using these keywords: {keywords}. The scenario should present a situation requiring a decision or response."
   )
   ```

4. **Simulate response**: Upon receiving the scenario, formulate how you would respond
   to the fictional situation. Consider:
   - What information would you need?
   - What tools or skills would you use?
   - What communication approach fits?
   - What could go wrong?

5. **Reflect on the simulation**:
   - Did the scenario reveal a gap in knowledge or skills?
   - Did the simulated response align with established preferences and guidelines?
   - Was there an unexpected insight from approaching a novel situation?

6. **Store dream fragment** (only if the reflection produced a genuine insight):
   ```
   mcp__arca_memory__memory_add(
     content="Sleep:Dream | {date} | Scenario: {brief_summary} | Insight: {extracted_insight}",
     bucket="mia-worklog"
   )
   ```

7. Connect the dream insight to any related real memories it illuminated:
   ```
   mcp__arca_memory__memory_connect(source_id=DREAM_ID, target_id=RELATED_ID, relationship_type="related_to")
   ```

## Scheduled Task Setup

The sleep cycle runs as a single cron-scheduled task. The onboarding process (see
[references/onboarding.md](./references/onboarding.md)) creates this task.

**Task contract:**
- Channel: `cli` (all scheduled task messages go to `cli`)
- Schedule: cron, user-configured (default: `0 2 * * *` — 2:00 AM daily)
- Timezone: from user preference (default: `Asia/Manila`)

**Response contract:**
```json
{"action": "continue", "message": "Sleep cycle completed — Read: {article_or_none} | Consolidated: {stats} | Reflections: {count} | Dream: {yes_or_skipped}"}
```

## Memory Design

All sleep-related memories live in `mia-worklog` with prefixed content:

| Prefix | Content pattern |
|--------|-----------------|
| `Sleep:Reading` | `{date} \| {article_title} \| Action: {action}` |
| `Sleep:Consolidation` | `{date} \| Deleted: N \| Merged: N \| Simplified: N \| Connected: N` |
| `Sleep:Config` | `schedule: {cron} \| timezone: {tz} \| reading_category: {cat}` |
| `Reflection:Positive` | `{date} \| {insight}` |
| `Reflection:Improve` | `{date} \| {insight}` |
| `Reflection:Pattern` | `{date} \| {insight}` |
| `Sleep:Dream` | `{date} \| Scenario: {summary} \| Insight: {insight}` |

Connect `Sleep:Reading` insights → incorporated memories with `derived_from`.
Connect `Reflection:*` → triggering memories with `derived_from`.
Connect `Sleep:Dream` → related real memories with `related_to`.
