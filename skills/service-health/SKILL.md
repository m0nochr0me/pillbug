---
name: service-health
description: Track HTTP health endpoints in a CSV and probe them with curl + jq. Use when the agent needs to (1) register or remove a service health URL, (2) run a one-off or batch liveness check with latency, (3) attach a freeform note describing what "alive" means for an endpoint, or (4) get aggregate stats like alive ratio and average latency. Requires curl and jq on PATH.
---

# Service Health

CSV-backed registry of HTTP health endpoints with a curl+jq probe and basic insights. All state lives under the workspace directory.

## Setup

Requires: `python3`, `curl`, `jq`.

The CLI is at: `scripts/service_health.py`

All invocations require `--workspace DIR` pointing to the agent's workspace root (e.g. `/opt/ext/pillbug/pillbug-voctiv/workspace`). The registry is persisted at `DIR/service_health.csv`.

## CLI

Show top-level and per-subcommand help:

```bash
python3 scripts/service_health.py --help
python3 scripts/service_health.py --workspace DIR check --help
```

### Add an endpoint

```bash
python3 scripts/service_health.py --workspace DIR add URL [--note TEXT]
```

- Use `--note` to record what "alive" means for this endpoint (e.g. `'curl + jq, alive if .status=="ok"'`).
- Duplicate endpoints are rejected.

### Update note / remove

```bash
python3 scripts/service_health.py --workspace DIR note URL "new commentary"
python3 scripts/service_health.py --workspace DIR remove URL
```

### Probe endpoints

```bash
python3 scripts/service_health.py --workspace DIR check               # check all
python3 scripts/service_health.py --workspace DIR check URL           # check one
python3 scripts/service_health.py --workspace DIR check --timeout 30
python3 scripts/service_health.py --workspace DIR check URL --jq '.status == "ok"'
```

- HTTP 2xx is the default alive criterion.
- `--jq EXPR` runs jq against the response body; a truthy result keeps the endpoint alive even if HTTP is 2xx, and a falsey result marks it dead. Invalid jq syntax exits 4 without mutating the row.
- `--timeout` is forwarded to `curl --max-time`; default is 120 seconds. Network or timeout failures count as `alive=false` with `latency_ms` measured up to the failure point.
- After every check the CSV row is updated with `last_checked`, `alive`, and `latency`.

### List / stats

```bash
python3 scripts/service_health.py --workspace DIR list
python3 scripts/service_health.py --workspace DIR stats
```

- `list` returns the current registry as JSON.
- `stats` returns totals, alive/dead/never-checked counts, alive ratio, latency min/max/avg, and the lists of currently down and never-checked endpoints.

## Data

`DIR/service_health.csv` — one row per endpoint, header on first line:

| column | meaning |
| --- | --- |
| `endpoint` | full URL of the health endpoint |
| `last_checked` | unix timestamp UTC of the most recent check (empty if never) |
| `alive` | `true` / `false` / empty |
| `latency` | integer milliseconds of the most recent check |
| `note` | freeform commentary (e.g. `'curl + jq, alive if "status"=="ok"'`) |

## Output and exit codes

- All commands print JSON to stdout on success.
- Errors are written to stderr as `{"error": "..."}` with non-zero exit codes: `2` usage / unknown endpoint, `3` environment (missing curl/jq, unreadable CSV), `4` check infrastructure failure (e.g. invalid jq expression).

## Notes

- Only the most recent probe result is retained per endpoint. If you need historical uptime data, run `check` periodically and snapshot the CSV externally.
- The CSV is rewritten atomically (`.csv.tmp` + rename) so concurrent reads see a consistent file.
- `--jq` is optional even though `jq` is required on PATH; the script will error with exit 3 if `jq` is missing only when `--jq` is supplied.
