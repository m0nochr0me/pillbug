# Sleep Cycle — Onboarding

This document guides the initial setup of the Sleep maintenance cycle. Follow each step
sequentially. The user must confirm preferences before the scheduled task is created.

## Prerequisites

Before onboarding, verify:

1. **Feed-reader has subscriptions**: Run the feed-reader list command to check if feeds
   are configured. If no feeds exist, guide the user to subscribe to at least one feed
   before proceeding.
   ```bash
   {WORKSPACE_DIR}/skills/feed-reader/scripts/feed_reader.sh \
     --workspace {WORKSPACE_DIR} list
   ```

2. **Arca-memory is accessible**: Call `mcp__arca_memory__memory_list_buckets()` and
   confirm a response. If unavailable, the sleep cycle cannot run.

3. **Existing sleep config**: Check if a sleep cycle is already configured:
   ```
   mcp__arca_memory__memory_get(query="sleep cycle configuration", bucket="mia-worklog")
   ```
   If found, inform the user and ask whether to reconfigure or keep existing settings.

## Step 1: Gather Preferences

Ask the user for the following. Use defaults in parentheses if the user does not specify:

### Schedule
- **Sleep time**: What time should the sleep cycle run? (`0 2 * * *` — daily at 2:00 AM)
- **Timezone**: Which timezone? (`Asia/Manila`)

### Bedtime Reading
- **Feed category**: Which RSS feed category should be used for bedtime reading?
  List available categories by parsing the feed subscriptions:
  ```bash
  {WORKSPACE_DIR}/skills/feed-reader/scripts/feed_reader.sh \
    --workspace {WORKSPACE_DIR} list
  ```
  The user picks one category. This can be changed later.

### Projection (REM Phase)
- **A2A peer**: Is there an A2A peer runtime available for dream generation?
  If yes, ask for the peer runtime ID. If no, projection phase will be skipped
  during sleep cycles until one is configured.

## Step 2: Store Configuration

Save the sleep configuration to memory:

```
mcp__arca_memory__memory_add(
  content="Sleep:Config | schedule: {cron_expression} | timezone: {timezone} | reading_category: {category} | a2a_peer: {peer_id_or_none} | created: {date}",
  bucket="mia-worklog"
)
```

## Step 3: Create the Scheduled Task

Create the sleep cycle task using the scheduler. The prompt must instruct the agent to
execute the full sleep cycle following the SKILL.md phases.

```
manage_agent_task(
  action="create",
  name="Sleep Maintenance Cycle",
  prompt="""Execute the Sleep maintenance cycle. Follow the sleep skill phases in order:

Phase 1 — Bedtime Reading:
- Retrieve sleep config from mia-worklog memory (query: "sleep cycle configuration").
- Use the configured reading category to list new posts via feed-reader.
- Pick and fetch one article. Read it, evaluate for useful knowledge.
- If useful: incorporate (new skill, memory entry, or AGENTS.md update). If not: skip.
- Log the reading to mia-worklog.

Phase 2 — Memory Consolidation:
- List all memory buckets.
- For each bucket, review recent entries (last 20).
- Delete obsolete entries (completed tasks, outdated statuses, stale context).
- Merge duplicate or highly similar memories into consolidated entries.
- Simplify verbose memories (>200 chars) without losing meaning.
- Create connections between related memories across buckets.
- Generalize when 3+ memories describe similar specific cases.
- Log consolidation stats to mia-worklog.

Phase 3 — Self-Reflection:
- Review recent worklog and client interactions.
- Identify 2-4 actionable insights (positive patterns, areas for improvement, observed preferences).
- Store reflection insights in mia-worklog with Reflection: prefix.
- Connect insights to triggering memories.

Phase 4 — Projection (REM):
- Check if A2A peer is configured in sleep config.
- If available: generate 3-5 keywords from recent context, request fictional scenario from peer.
- Simulate response to the scenario, reflect on what it revealed.
- Store dream fragment only if a genuine insight emerged.
- If no A2A peer: skip and log.

After all phases, respond with:
{"action": "continue", "message": "Sleep cycle completed — Read: {article_or_none} | Consolidated: {stats} | Reflections: {count} | Dream: {yes_or_skipped}"}""",
  schedule_type="cron",
  cron_expression="{user_cron_expression}",
)
```

**Important:** The task sends messages to channel `cli`. This is the default for scheduled
tasks and does not need to be explicitly configured.

## Step 4: Confirm Setup

After creating the task, confirm to the user:

1. Display the configured schedule in human-readable form
2. Show the reading category
3. Note A2A projection status (enabled with peer ID, or skipped)
4. Mention they can:
   - Run manually anytime by asking for a sleep cycle
   - Pause with: "pause sleep cycle"
   - Change reading category: "change bedtime reading to {category}"
   - Check logs: "show sleep logs"

## Step 5: Initial Dry Run (Optional)

Offer to run the sleep cycle immediately as a test. This lets the user verify:
- Feed-reader integration works
- Memory consolidation runs without errors
- Reflections are generated appropriately
- A2A projection connects (if configured)

If the user accepts, execute the full cycle per SKILL.md phases and report results.

## Reconfiguration

When the user wants to change sleep settings:

1. Retrieve existing config: `mcp__arca_memory__memory_get(query="Sleep:Config", bucket="mia-worklog")`
2. Retrieve existing task: `manage_agent_task(action="list")` — find "Sleep Maintenance Cycle"
3. Apply changes:
   - **Schedule change**: `manage_agent_task(action="update", task_id=ID, cron_expression=NEW)`
   - **Category change**: Update the `Sleep:Config` memory (delete old, add new)
   - **A2A peer change**: Update the `Sleep:Config` memory
4. Confirm changes to the user

## Troubleshooting

| Issue | Resolution |
|-------|------------|
| No new posts in category | Try a different category, or check if feeds need new subscriptions |
| Memory consolidation takes too long | Reduce `n` in `memory_get_last` to 10 for large buckets |
| A2A peer unreachable | Log skip, continue with phases 1-3. Suggest checking peer status |
| Task not firing | Check `manage_agent_task(action="get", task_id=ID)` for `last_run` status |
| Duplicate sleep tasks | List tasks, delete extras, keep one |
