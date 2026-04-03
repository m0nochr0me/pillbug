---
name: feed-reader
description: RSS/Atom feed reader for subscribing to feeds, checking for new posts, and saving post content to text files. Use when the agent needs to: (1) subscribe to an RSS or Atom feed URL, (2) list new/updated post titles from subscribed feeds, (3) fetch and save full post text to disk, or (4) manage feed subscriptions by category.
---

# Feed Reader

Bash+curl+jq tool for RSS/Atom feed management. Config and output live under the workspace directory.

## Setup

Requires: `bash`, `curl`, `jq`, `python3` (for XML parsing and HTML stripping).

The script is at: `scripts/feed_reader.sh`

All commands require `--workspace DIR` pointing to the agent's workspace root (e.g. `~/.pillbug/workspace`).

## Commands

### Subscribe to a feed

```bash
scripts/feed_reader.sh --workspace DIR subscribe FEED_URL [CATEGORY]
```

- `CATEGORY` defaults to `uncategorized`
- Duplicates are detected and skipped
- Subscriptions are saved in `DIR/feeds.json`

### List new post titles

```bash
scripts/feed_reader.sh --workspace DIR list [CATEGORY]
```

- Omit `CATEGORY` to check all feeds
- Only shows posts newer than the last check (first run shows all)
- Already-fetched posts are excluded from results
- Output is numbered (1-based) and truncated to 16 items
- Updates `last_checked` timestamp per feed after each run

### Fetch post content to file

```bash
scripts/feed_reader.sh --workspace DIR fetch NUMBER
scripts/feed_reader.sh --workspace DIR fetch "SEARCH_TERM"
scripts/feed_reader.sh --workspace DIR fetch FEED_URL
```

- Fetch by post number from the last `list` output (e.g. `fetch 2`)
- Search by title substring (case-insensitive) or by feed URL (saves all posts)
- Saves each matched post as a `.txt` file in `DIR/fetched/`
- Files contain title, link, date, and HTML-stripped body text

## Data

- `feeds.json` — subscription list with categories and `last_checked` timestamps
- `last_list.json` — cached results from the last `list` run (for fetch-by-number)
- `fetched/*.txt` — saved post content
