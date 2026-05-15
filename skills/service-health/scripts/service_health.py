#!/usr/bin/env python3
"""Service health check CLI for the Pillbug `service-health` skill.

Maintains a CSV registry of HTTP health endpoints, probes them with curl,
optionally validates response bodies with jq, and reports insights.

CSV schema (one row per endpoint, header on first line):
    endpoint,last_checked,alive,latency,note

  endpoint      — full URL of the health endpoint
  last_checked  — unix timestamp UTC of the most recent check (empty if never)
  alive         — "true" | "false" | "" (empty if never checked)
  latency       — integer milliseconds of the most recent check ("" if never)
  note          — freeform commentary (e.g. 'curl + jq, alive if "status"=="ok"')

Exit codes: 0 ok, 2 usage, 3 environment (curl/jq missing, CSV unreadable),
4 check failure that is not the endpoint being down (e.g. invalid jq).
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

CSV_NAME = "service_health.csv"
FIELDS = ("endpoint", "last_checked", "alive", "latency", "note")
DEFAULT_TIMEOUT = 120


def _die(code: int, message: str) -> None:
    json.dump({"error": message}, sys.stderr)
    sys.stderr.write("\n")
    sys.exit(code)


def _csv_path(workspace: Path) -> Path:
    return workspace / CSV_NAME


def _require_tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        _die(3, f"required tool not found on PATH: {name}")
    return found


@dataclass
class Row:
    endpoint: str
    last_checked: str = ""
    alive: str = ""
    latency: str = ""
    note: str = ""

    def to_csv(self) -> dict[str, str]:
        return asdict(self)

    def to_view(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "last_checked": int(self.last_checked) if self.last_checked else None,
            "alive": {"true": True, "false": False}.get(self.alive, None),
            "latency_ms": int(self.latency) if self.latency else None,
            "note": self.note,
        }


def _load_rows(workspace: Path) -> list[Row]:
    path = _csv_path(workspace)
    if not path.is_file():
        return []
    try:
        with path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            missing = [f for f in FIELDS if f not in (reader.fieldnames or ())]
            if missing:
                _die(3, f"CSV at {path} is missing fields: {missing}")
            return [Row(**{f: (raw.get(f) or "") for f in FIELDS}) for raw in reader]
    except OSError as exc:
        _die(3, f"failed to read {path}: {exc}")
    return []  # unreachable, satisfies type checker


def _write_rows(workspace: Path, rows: list[Row]) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    path = _csv_path(workspace)
    tmp = path.with_suffix(".csv.tmp")
    try:
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(FIELDS))
            writer.writeheader()
            for row in rows:
                writer.writerow(row.to_csv())
        tmp.replace(path)
    except OSError as exc:
        _die(3, f"failed to write {path}: {exc}")


def _find(rows: list[Row], endpoint: str) -> int:
    for index, row in enumerate(rows):
        if row.endpoint == endpoint:
            return index
    return -1


def _run_curl(curl: str, url: str, timeout: int) -> tuple[int | None, int, str]:
    """Return (http_status_or_None, latency_ms, body). status None on network failure."""
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [
                curl,
                "--silent",
                "--show-error",
                "--location",
                "--max-time",
                str(timeout),
                "--write-out",
                "\n__PB_STATUS__:%{http_code}",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except subprocess.TimeoutExpired:
        elapsed = int((time.perf_counter() - started) * 1000)
        return None, elapsed, ""
    elapsed = int((time.perf_counter() - started) * 1000)
    output = completed.stdout
    marker = "\n__PB_STATUS__:"
    body, _, tail = output.rpartition(marker)
    if not tail:
        return None, elapsed, ""
    try:
        status = int(tail.strip())
    except ValueError:
        status = None
    return status, elapsed, body


def _eval_jq(jq: str, expr: str, body: str) -> tuple[bool | None, str | None]:
    """Run jq EXPR against body. Returns (truthy, error). truthy=None on jq error."""
    try:
        completed = subprocess.run(
            [jq, "-e", expr],
            input=body,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return None, "jq timed out"
    if completed.returncode == 0:
        return True, None
    if completed.returncode == 1:
        # jq ran successfully but expression was false/null
        return False, None
    return None, completed.stderr.strip() or f"jq exit {completed.returncode}"


def cmd_add(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).expanduser().resolve()
    rows = _load_rows(workspace)
    if _find(rows, args.endpoint) >= 0:
        _die(2, f"endpoint already tracked: {args.endpoint}")
    rows.append(Row(endpoint=args.endpoint, note=args.note or ""))
    _write_rows(workspace, rows)
    json.dump({"added": args.endpoint, "note": args.note or ""}, sys.stdout)
    sys.stdout.write("\n")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).expanduser().resolve()
    rows = _load_rows(workspace)
    index = _find(rows, args.endpoint)
    if index < 0:
        _die(2, f"endpoint not tracked: {args.endpoint}")
    removed = rows.pop(index)
    _write_rows(workspace, rows)
    json.dump({"removed": removed.endpoint}, sys.stdout)
    sys.stdout.write("\n")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).expanduser().resolve()
    rows = _load_rows(workspace)
    index = _find(rows, args.endpoint)
    if index < 0:
        _die(2, f"endpoint not tracked: {args.endpoint}")
    rows[index].note = args.text
    _write_rows(workspace, rows)
    json.dump({"endpoint": args.endpoint, "note": args.text}, sys.stdout)
    sys.stdout.write("\n")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).expanduser().resolve()
    rows = _load_rows(workspace)
    json.dump(
        {"workspace": str(workspace), "count": len(rows), "endpoints": [r.to_view() for r in rows]},
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).expanduser().resolve()
    rows = _load_rows(workspace)
    if not rows:
        _die(2, "no endpoints tracked; add one first with `add`")

    targets: list[int]
    if args.endpoint:
        index = _find(rows, args.endpoint)
        if index < 0:
            _die(2, f"endpoint not tracked: {args.endpoint}")
        targets = [index]
    else:
        targets = list(range(len(rows)))

    curl = _require_tool("curl")
    jq = _require_tool("jq") if args.jq else ""

    results: list[dict[str, object]] = []
    now = int(time.time())
    for index in targets:
        row = rows[index]
        status, latency_ms, body = _run_curl(curl, row.endpoint, args.timeout)
        alive: bool
        detail: str
        if status is None:
            alive = False
            detail = "network error or timeout"
        elif not (200 <= status < 300):
            alive = False
            detail = f"http {status}"
        elif args.jq:
            truthy, jq_error = _eval_jq(jq, args.jq, body)
            if truthy is None:
                _die(4, f"jq failed for {row.endpoint}: {jq_error}")
            alive = bool(truthy)
            detail = f"http {status}, jq {'pass' if alive else 'fail'}"
        else:
            alive = True
            detail = f"http {status}"

        row.last_checked = str(now)
        row.alive = "true" if alive else "false"
        row.latency = str(latency_ms)
        results.append(
            {
                "endpoint": row.endpoint,
                "alive": alive,
                "http_status": status,
                "latency_ms": latency_ms,
                "detail": detail,
            }
        )

    _write_rows(workspace, rows)
    json.dump({"checked_at": now, "count": len(results), "results": results}, sys.stdout)
    sys.stdout.write("\n")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).expanduser().resolve()
    rows = _load_rows(workspace)
    total = len(rows)
    checked = [r for r in rows if r.last_checked]
    alive = [r for r in checked if r.alive == "true"]
    dead = [r for r in checked if r.alive == "false"]
    never = [r for r in rows if not r.last_checked]
    latencies = [int(r.latency) for r in checked if r.latency]
    summary = {
        "workspace": str(workspace),
        "total": total,
        "checked": len(checked),
        "alive": len(alive),
        "dead": len(dead),
        "never_checked": len(never),
        "alive_ratio": (len(alive) / len(checked)) if checked else None,
        "latency_ms": {
            "min": min(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
            "avg": (sum(latencies) // len(latencies)) if latencies else None,
        },
        "down": [r.endpoint for r in dead],
        "stale": [r.endpoint for r in never],
    }
    json.dump(summary, sys.stdout)
    sys.stdout.write("\n")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="service_health.py",
        description="Track HTTP health endpoints in a CSV and probe them with curl + jq.",
    )
    parser.add_argument(
        "--workspace",
        "-w",
        required=True,
        help="workspace directory holding service_health.csv (e.g. ~/.pillbug/workspace)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="add an endpoint to the registry")
    p_add.add_argument("endpoint", help="full URL of the health endpoint")
    p_add.add_argument("--note", default="", help="freeform commentary describing alive criteria")
    p_add.set_defaults(func=cmd_add)

    p_rm = sub.add_parser("remove", help="remove an endpoint from the registry")
    p_rm.add_argument("endpoint", help="endpoint URL to remove (exact match)")
    p_rm.set_defaults(func=cmd_remove)

    p_note = sub.add_parser("note", help="update the note for an endpoint")
    p_note.add_argument("endpoint", help="endpoint URL (exact match)")
    p_note.add_argument("text", help="new note text")
    p_note.set_defaults(func=cmd_note)

    p_list = sub.add_parser("list", help="list all tracked endpoints with current state")
    p_list.set_defaults(func=cmd_list)

    p_check = sub.add_parser(
        "check",
        help="probe one or all endpoints; HTTP 2xx is alive unless --jq overrides",
    )
    p_check.add_argument(
        "endpoint",
        nargs="?",
        default=None,
        help="endpoint to check (omit to check all)",
    )
    p_check.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"connect+read timeout seconds passed to curl --max-time (default {DEFAULT_TIMEOUT})",
    )
    p_check.add_argument(
        "--jq",
        default=None,
        help='jq filter that must evaluate to a truthy value for "alive" (e.g. \'.status == "ok"\')',
    )
    p_check.set_defaults(func=cmd_check)

    p_stats = sub.add_parser("stats", help="summarize registry health and latency")
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "timeout", None) is not None and args.timeout < 1:
        _die(2, "--timeout must be at least 1")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
