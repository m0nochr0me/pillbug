# Curation Procedure — Detailed Mechanics

## Contents

1. [Temporal Marker Catalog](#1-temporal-marker-catalog)
2. [Re-anchoring Decisions](#2-re-anchoring-decisions)
3. [Edge-Preserving Replacement](#3-edge-preserving-replacement)
4. [Continuity Clustering](#4-continuity-clustering)
5. [Conflict Resolution](#5-conflict-resolution)
6. [Tuning Parameters](#6-tuning-parameters)

---

## 1. Temporal Marker Catalog

Scan memory text for these markers; each implies a freshness check against the current date.

| Marker class        | Examples                                                                   | Why it rots                            |
| ------------------- | -------------------------------------------------------------------------- | -------------------------------------- |
| Future intent       | "will", "plans to", "is going to", "upcoming", "next week/month/year"      | The event eventually happens or passes |
| Relative date       | "tomorrow", "in 3 months", "last Tuesday", "recently"                      | Meaning drifts away from `created_at`  |
| Named future date   | "in July", "on the 14th", "this summer", a calendar date ahead of creation | Becomes past once that date arrives    |
| Present-state claim | "currently", "now", "these days", "is 34", "lives in", "works at"          | Silently presented as eternally true   |
| Duration / progress | "3 months into", "halfway through", "since January"                        | The number keeps changing              |
| Status word         | "ongoing", "in progress", "pending", "open"                                | Resolves over time                     |

Memories with **no** temporal marker (stable traits, long-standing preferences) are not freshness candidates — leave them for Continuity/Relevance.

---

## 2. Re-anchoring Decisions

For each temporal memory, pick exactly one action:

| Situation                                                 | Action                                           | Example result                                                         |
| --------------------------------------------------------- | ------------------------------------------------ | ---------------------------------------------------------------------- |
| Future event, date now passed, outcome recorded elsewhere | Rewrite future → past as fact                    | "traveled to Singapore in July 2026"                                   |
| Future event, date now passed, outcome **unknown**        | Rewrite to planned-but-unconfirmed + flag        | "had planned to travel to Singapore in July 2026; outcome unconfirmed" |
| Future event still in the future                          | Leave; only convert a relative date to absolute  | "next Friday" → "on 2026-06-12"                                        |
| Relative date                                             | Resolve against `created_at` to an absolute date | "recently moved" (created 2026-05-20) → "moved around 2026-05"         |
| Present-state claim aged past staleness window            | Re-stamp with as-of date + flag for refresh      | "lives in Berlin (as of 2026-05); confirm still current"               |
| Present-state claim still fresh                           | Leave                                            | —                                                                      |

**Outcome grounding rule:** only assert that a planned event happened if a _different_ memory records the outcome. Absent that, the event is "planned / unconfirmed." Never upgrade a plan to a completed fact on the calendar alone.

**Staleness window** for present-state claims: default 180 days since `created_at` (see Tuning). Past that, re-stamp and flag rather than trusting it silently.

---

## 3. Edge-Preserving Replacement

Arca has no in-place update, so re-anchoring and synthesis both go through add-then-retire. Never `memory_delete` a node before capturing its edges.

```
# 1. read the original's neighborhood
edges = memory_traverse(original_id, depth=1)

# 2. add the updated/synthesized node carrying the original's outgoing edges
new_id = memory_add(
    content=UPDATED_TEXT,
    bucket=SAME_BUCKET,
    connected_nodes=[e.target for e in edges.outgoing],
    relationship_types=[e.type for e in edges.outgoing],
)

# 3a. provenance matters (uncertain change, audit value):
memory_connect(source_id=new_id, target_id=original_id, relationship_type="supersedes")
#     keep the original, marked as superseded.

# 3b. clean replacement (clear stale fact, no audit value):
memory_delete(original_id)
```

- Re-point **inbound** edges too: for each node that pointed at the original, `memory_connect` it to `new_id` with the same `relationship_type`, then (3b path) the dangling edge dies with the deleted original.
- When two edges of the same `relationship_type` would point to the same target, keep one.
- Prefer path **3a (supersedes, keep original)** for freshness re-anchoring where the outcome was uncertain or the user might want to see the history; prefer **3b (delete)** for clearly stale, now-false statements that would otherwise contradict the re-anchored fact.

---

## 4. Continuity Clustering

How to decide what becomes a canonical `Profile:` fact:

1. **Group by subject, not by wording.** Fragments about cameras, lenses, and shooting style all belong to one "photography setup" cluster even if phrased differently.
2. **Signal threshold:** synthesize when a subject has ≥2 corroborating fragments, or 1 fragment that is clearly a durable trait (e.g. an explicit profession). One-off passing mentions stay as raw fragments — they are context, not profile.
3. **Compose the durable core.** Strip the conversational framing; keep the stable fact. "I was thinking maybe a Sony would be nice" + "got the a7 IV finally" → `Profile: Shoots on a Sony a7 IV (E-mount).`
4. **Refresh, don't duplicate.** If a `Profile:` node for the subject exists, replace it via §3 carrying its `derived_from` links; do not add a second.
5. **Link provenance:** every contributing fragment gets `derived_from` → the `Profile:` node, so the synthesis stays traceable and the next pass can re-derive it.

A `Profile:` fact should read as something the agent could confidently open a new conversation with.

---

## 5. Conflict Resolution

When two memories disagree, resolve by **recency and specificity**:

| Conflict                                          | Resolution                                                   |
| ------------------------------------------------- | ------------------------------------------------------------ |
| Newer statement contradicts older                 | Newer wins; supersede/retire the older                       |
| Preference reversed ("no longer need X")          | Retire the old `Preference:`/`Constraint:`; do not keep both |
| Two compatible preferences                        | Keep both                                                    |
| Constraint vs. preference clash                   | Constraint (hard requirement) outranks preference (soft)     |
| User correction of the summary                    | Always wins over any synthesized inference                   |
| Genuinely ambiguous / can't tell which is current | Do not guess — flag both for user confirmation               |

Record every reconciliation in the run summary so a wrong call is visible and reversible.

---

## 6. Tuning Parameters

| Parameter                               | Default                                            | When to adjust                                                                    |
| --------------------------------------- | -------------------------------------------------- | --------------------------------------------------------------------------------- |
| Staleness window (present-state claims) | 180 days                                           | Shorten for fast-moving facts (jobs, location); lengthen for slow traits          |
| Synthesis signal threshold              | 2 fragments                                        | Raise to 3 to be conservative; 1 only for explicit durable traits                 |
| Scope buckets                           | user/personal/client buckets                       | Add buckets that hold user facts; never include pure project/architecture buckets |
| Provenance mode                         | supersedes for uncertain, delete for clearly stale | Use supersedes everywhere when the operator wants a full audit trail              |
| Schedule                                | a few times/week (`0 3 * * 1,4`)                   | Nightly only for very active users; weekly for light usage                        |

The user model changes slowly — bias toward conservative, well-flagged curation over aggressive rewriting.
