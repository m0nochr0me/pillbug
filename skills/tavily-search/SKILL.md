---
name: tavily-search
description: Search the web with the Tavily Search API through a bundled shell script. Use when the agent needs live web search results, wants a simple CLI wrapper around Tavily, or needs normalized JSON output with null fields and empty arrays removed.
---

# Tavily Search

Search the web with Tavily using the bundled shell script. The script accepts a single argument: the search query. Authentication and search tuning come from environment variables.

## Usage

Run the search script with the query as a single shell argument:

```bash
bash .github/skills/tavily-search/scripts/search.sh "pillbug github"
```

The script posts to Tavily's `/search` endpoint and prints JSON with `null` fields and empty arrays removed before returning it.

## Environment

- `TAVILY_API_KEY` is required.
- `TAVILY_DEPTH` controls `search_depth`. Defaults to `basic` when unset.
- `TAVILY_TOP_K` controls `max_results`. Defaults to `5` when unset.

## Notes

- `curl` and `jq` must already be installed.
- Quote multi-word queries so they are passed as the single required argument.
- On API failure, the script prints Tavily's error payload to stderr and exits non-zero.
