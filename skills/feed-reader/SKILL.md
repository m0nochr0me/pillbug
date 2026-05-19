---
name: feed-reader
description: RSS/Atom/RDF feed reader for subscribing to feeds, checking for new posts, and saving post content to text files. Use when the agent needs to: (1) subscribe to an RSS, Atom, or RSS 1.0 (RDF) feed URL, (2) list new/updated post titles from subscribed feeds, (3) fetch and save full post text to disk, or (4) manage feed subscriptions by category.
---

# Feed Reader

Python CLI for RSS/Atom/RDF feed management. Stdlib only. Config and output live under the workspace directory.

## Setup

Requires: `python3` (3.10+). No external packages.

The script is at: `scripts/feed_reader.py`

All commands require `--workspace DIR` pointing to the agent's workspace root (e.g. `~/.pillbug/workspace`).

## Commands

### Subscribe to a feed

```bash
python3 scripts/feed_reader.py --workspace DIR subscribe FEED_URL [CATEGORY]
```

- Supports RSS 2.0, Atom 1.0, and RSS 1.0 (RDF) feeds
- `CATEGORY` defaults to `uncategorized`
- Duplicates are detected and skipped
- Subscriptions are saved in `DIR/feeds.json`

### List new post titles

```bash
python3 scripts/feed_reader.py --workspace DIR list [CATEGORY]
```

- Omit `CATEGORY` to check all feeds
- Only shows posts newer than the last check (first run shows all)
- Already-fetched posts are excluded from results
- Output is numbered (1-based) and truncated to 16 items; format: `N. [post_link] title`
- Updates `last_checked` timestamp per feed after each run

### Fetch post content to file

```bash
python3 scripts/feed_reader.py --workspace DIR fetch NUMBER
python3 scripts/feed_reader.py --workspace DIR fetch "SEARCH_TERM"
python3 scripts/feed_reader.py --workspace DIR fetch FEED_URL
```

- Fetch by post number from the last `list` output (e.g. `fetch 2`)
- Search by title substring (case-insensitive) or by feed URL (saves all posts from that feed)
- Saves each matched post as a `.txt` file in `DIR/fetched/`
- Files contain title, link, date, and HTML-stripped body text

## Data

- `feeds.json` — subscription list with categories and `last_checked` timestamps
- `last_list.json` — cached results from the last `list` run (powers fetch-by-number)
- `fetched/*.txt` — saved post content
