---
name: dreaming
description: "Background memory curation that keeps the agent's model of the user fresh, continuous, and relevant — inspired by ChatGPT's 'dreaming' memory synthesis. Reviews accumulated memories across conversations and (1) re-anchors time-bound facts to the present (freshness: 'traveling to Singapore in July' becomes 'traveled to Singapore in July 2026' after the trip), (2) synthesizes scattered fragments into one coherent, current user model that carries forward into new chats (continuity), and (3) maintains an explicit, conflict-free set of the user's preferences and constraints (relevance). Triggers on: 'run dreaming', 'dream', 'curate/refresh memory', 'keep memories current', 'update what you know about me', 'memory summary', 'what do you know about me', 'synthesize my profile', or a scheduled background curation pass. This is memory synthesis, not fictional dream generation."
---

# Dreaming — User-Model Memory Curation

A background pass that curates what the agent knows about the user so it stays **fresh, continuous, and relevant** — the same goals OpenAI describes for ChatGPT's "dreaming" memory. It works over the Arca memory graph, synthesizing accumulated fragments into a coherent, up-to-date user model without the user having to say "remember this."

| Pillar         | Problem it fixes                               | What this skill does                                                                                           |
| -------------- | ---------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| **Freshness**  | Facts go stale as time passes                  | Re-anchor time-bound memories to the present (future plans become past events; relative dates become absolute) |
| **Continuity** | Context is scattered across many chats         | Synthesize fragments into canonical `Profile:` facts that carry forward into new conversations                 |
| **Relevance**  | Preferences/constraints get lost or contradict | Maintain an explicit, conflict-free set of `Preference:` and `Constraint:` facts the agent applies             |

This skill never invents facts — it only curates ones the user has actually provided.

## Scope — what to curate

Curate memories that describe **the user and their world**: plans, situations, relationships, possessions/gear, ongoing projects, stated preferences and constraints. Skip purely technical or project-internal memories (architecture notes, decisions, code facts) — those belong to project buckets, not the user model.

This skill re-anchors and synthesizes memories; it does not delete memories simply because they have aged.

## Session start

Before curating:

1. `mcp__arca_memory__memory_list_buckets` — orient. Identify which bucket(s) hold user facts (commonly a personal / client / user bucket) versus project buckets.
2. `mcp__arca_memory__memory_get(query="dreaming config and last run", bucket=WORKLOG)` — load config (scope buckets, schedule) and when curation last ran, so you only review what changed since.
3. Note the current date — every freshness judgment is relative to it.

## Tooling note — Arca has no in-place update

To change a memory's text you **add the updated node, then delete the original**. Deleting a node drops its edges, so before deleting:

1. `mcp__arca_memory__memory_traverse(memory_id, depth=1)` to read the original's edges.
2. `mcp__arca_memory__memory_add(content=UPDATED, bucket=SAME, connected_nodes=[...], relationship_types=[...])` carrying that edge set.
3. When provenance matters, instead connect the new node `supersedes` the old and keep the old; otherwise `mcp__arca_memory__memory_delete(original_id)`.

Detailed edge-preservation and conflict mechanics: read [references/curation-procedure.md](references/curation-procedure.md).

## Pass A — Freshness (temporal re-anchoring)

Find time-bound memories whose tense or framing no longer matches the calendar, and rewrite them to the present.

1. Pull user-scope memories: `mcp__arca_memory__memory_get_last(n=50, bucket=BUCKET)` and targeted `memory_get` queries for time words ("will", "planning", "next", "currently", month/year names).
2. For each, locate its time reference and compare to the current date:
   - **Future plan whose date has passed** → rewrite future to past. _"User is traveling to Singapore in July"_ (created 2026-05) → _"User traveled to Singapore in July 2026."_ If the outcome is not actually recorded anywhere, do not assert it happened — write _"User had planned to travel to Singapore in July 2026; outcome unconfirmed"_ and flag it in the summary for the user to confirm.
   - **Relative date** ("next week", "in 3 months", "tomorrow") → resolve to an absolute date using the memory's `created_at`, so it never silently rots.
   - **Present-state claim** ("currently living in…", "is 34", "new job at…") that has aged past the staleness window → re-stamp with an as-of date (_"…as of 2026-05"_) rather than letting it read as eternally true; flag long-aged ones for refresh.
3. Apply the change with the add-then-delete (or `supersedes`) mechanic above. Record each re-anchoring in the run summary.

The full temporal-marker catalog and decision rules are in [references/curation-procedure.md](references/curation-procedure.md).

## Pass B — Continuity (cross-conversation synthesis)

Turn many scattered fragments into a small set of durable, current `Profile:` facts the agent can lead with in any new chat.

1. Cluster user-scope memories by subject (e.g. "photography setup", "travel style", "current projects", "family", "health").
2. For each cluster with real signal (roughly 2+ corroborating fragments), compose one canonical synthesized fact:
   ```
   Profile: User shoots wildlife on a Sony a7 IV (E-mount); prioritizes long telephoto reach and low-light AF.
   ```
   Refresh the existing `Profile:` node for that subject if one already exists rather than adding a duplicate.
3. Connect each source fragment to the canonical node:
   `mcp__arca_memory__memory_connect(source_id=FRAGMENT, target_id=PROFILE, relationship_type="derived_from")`.
4. When the synthesis makes a fragment redundant or outdated, supersede it (see references). Keep fragments that still carry unique detail.

Synthesize the durable signal, not one-off chatter. A passing mention is context; a pattern across conversations is a `Profile:` fact.

## Pass C — Relevance (preferences & constraints)

Maintain the personalization layer: an explicit, non-contradictory set of preferences and hard constraints that future responses must honor.

1. Identify durable preference/constraint statements across memories:
   - `Preference: Prefers wildlife photography and quiet, nature-forward destinations.`
   - `Constraint: Hotels must have air conditioning.`
2. Promote them to dedicated `Preference:` / `Constraint:` nodes in the user bucket (one fact each).
3. Resolve conflicts by recency — the newer statement wins:
   - A reversed preference (_"I no longer need AC"_) **supersedes and retires** the old constraint.
   - Two compatible preferences both stay; two contradictory ones get reconciled to the most recent, with the change noted in the summary.
4. These nodes are what make recommendations specific (camera gear that fits the user's mount, an AC hotel near wildlife) instead of generic.

## Memory summary — the reviewable surface

ChatGPT exposes a "memory summary page" the user can read and edit. Provide the same: on request (_"what do you know about me"_, _"memory summary"_) or at the end of a curation pass, present a concise digest grouped as **Profile / Preferences / Constraints / Active situations (with their as-of dates)**. Keep it short and correctable — invite the user to fix anything wrong, then apply their corrections as memory edits. Optionally persist the digest to a single canonical `Profile:summary` node so it is queryable.

## Safety rules

- **Never fabricate.** Every curated fact must trace to memories the user actually provided. Unknown outcomes are written as uncertain and flagged, never asserted.
- **Preserve information on edit.** Carry edges (or use `supersedes`) before deleting; don't drop unique detail when synthesizing.
- **Autonomous vs. confirmed.** Additions, re-anchoring, and synthesis are safe to run in the background. Before _deleting_ a load-bearing node (one with a `Decision:`/`Constraint:`/`Goal:` prefix or 2+ edges), confirm with the operator — or use `supersedes` instead of delete.
- **Honor corrections immediately.** When the user edits the summary, that correction outranks any synthesized inference.

## Scheduled setup (background process)

Dreaming is meant to run quietly in the background. Register it as a scheduled task on channel `cli`:

- Use `manage_agent_task(action="create", ...)` with a cron schedule (a sensible default is a few times a week, e.g. `0 3 * * 1,4` — Mon/Thu 03:00; lighter than nightly because the user model moves slowly).
- Store the chosen schedule and scope buckets in a `Dreaming:Config` memory so future runs and `manage_agent_task(action="update", ...)` stay consistent.
- Pause/resume via `manage_agent_task(action="update", enabled=False/True)`.

**Response contract (scheduled run):**

```json
{
  "action": "continue",
  "message": "Dreaming pass — Re-anchored: {n} | Synthesized: {n} profiles | Preferences/Constraints updated: {n} | Flagged for confirmation: {n}"
}
```

## Report format

After a pass, produce a concise summary:

```
Dreaming Curation Report
------------------------
Scope:           {buckets}, changes since {last_run}
Freshness:       X memories re-anchored (future to past / relative to absolute / re-stamped)
Continuity:      Y profiles synthesized or refreshed, Z fragments linked
Relevance:       P preferences, C constraints updated; K conflicts reconciled
Flagged:         N items need user confirmation (unverified outcomes / contradictions)
```

Then log it: `mcp__arca_memory__memory_add(content="Dreaming:Log | {date} | re-anchored:X synth:Y pref:P constr:C flagged:N", bucket=WORKLOG)`.

## Memory design — prefixes

| Prefix            | Role                                                     |
| ----------------- | -------------------------------------------------------- |
| `Profile:`        | Synthesized durable user-model fact (continuity output)  |
| `Preference:`     | A user preference the agent should follow (relevance)    |
| `Constraint:`     | A hard requirement the agent must honor (relevance)      |
| `Profile:summary` | Optional single canonical "what I know about you" digest |
| `Dreaming:Config` | Schedule + scope buckets for the background pass         |
| `Dreaming:Log`    | Per-run curation log                                     |

Connect source fragments to synthesized facts with `derived_from`; connect a replacement to the memory it replaces with `supersedes`.
