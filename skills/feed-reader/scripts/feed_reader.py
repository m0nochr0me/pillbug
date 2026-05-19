#!/usr/bin/env python3
"""RSS/Atom/RDF feed reader CLI for the Pillbug `feed-reader` skill.

Subscribe to feeds, list new post titles, and fetch full post content to disk.
Supports RSS 2.0, Atom 1.0, and RSS 1.0 (RDF). Stdlib only.

Data (all under --workspace DIR):
  feeds.json       — subscription list with categories and last_checked timestamps
  last_list.json   — cached results from the last `list` run (powers fetch-by-number)
  fetched/*.txt    — saved post content

Exit codes: 0 ok, 2 usage error.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

USER_AGENT = "feed-reader/2.0"
FETCH_TIMEOUT = 30
LIST_LIMIT = 16

ATOM_NS = "http://www.w3.org/2005/Atom"
RSS1_NS = "http://purl.org/rss/1.0/"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
DC_NS = "http://purl.org/dc/elements/1.1/"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"


def _die(code: int, message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_filename(title: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", title)[:200]


def _strip_html(raw: str) -> str:
    text = html.unescape(raw or "")
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _parse_date(raw: str) -> str:
    """Normalize an RSS/Atom date string to ISO 8601 UTC. Returns '' on failure."""
    if not raw:
        return ""
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except TypeError, ValueError:
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass
    return ""


def _fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return resp.read()


def _parse_atom(root: ET.Element) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for entry in root.findall(f".//{{{ATOM_NS}}}entry"):
        title = (entry.findtext(f"{{{ATOM_NS}}}title") or "").strip()
        link = ""
        for link_el in entry.findall(f"{{{ATOM_NS}}}link"):
            if link_el.get("rel", "alternate") == "alternate":
                link = link_el.get("href", "") or ""
                break
        pub = entry.findtext(f"{{{ATOM_NS}}}updated") or entry.findtext(f"{{{ATOM_NS}}}published") or ""
        desc = entry.findtext(f"{{{ATOM_NS}}}summary") or entry.findtext(f"{{{ATOM_NS}}}content") or ""
        items.append({"title": title, "link": link, "pubDate": _parse_date(pub), "description": desc})
    return items


def _parse_rss2(root: ET.Element) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or item.findtext(f"{{{DC_NS}}}date") or "").strip()
        desc = item.findtext(f"{{{CONTENT_NS}}}encoded") or item.findtext("description") or ""
        items.append({"title": title, "link": link, "pubDate": _parse_date(pub), "description": desc})
    return items


def _parse_rss1(root: ET.Element) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for item in root.findall(f".//{{{RSS1_NS}}}item"):
        title = (item.findtext(f"{{{RSS1_NS}}}title") or "").strip()
        link = (item.findtext(f"{{{RSS1_NS}}}link") or "").strip()
        pub = (item.findtext(f"{{{DC_NS}}}date") or "").strip()
        desc = item.findtext(f"{{{CONTENT_NS}}}encoded") or item.findtext(f"{{{RSS1_NS}}}description") or ""
        items.append({"title": title, "link": link, "pubDate": _parse_date(pub), "description": desc})
    return items


def parse_feed(url: str) -> list[dict[str, str]]:
    try:
        raw = _fetch_bytes(url)
    except urllib.error.URLError, TimeoutError, OSError:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    tag = root.tag
    if tag == f"{{{ATOM_NS}}}feed":
        return _parse_atom(root)
    if tag == f"{{{RDF_NS}}}RDF":
        return _parse_rss1(root)
    return _parse_rss2(root)


def _load_feeds(workspace: Path) -> dict[str, list[dict[str, Any]]]:
    path = workspace / "feeds.json"
    if not path.is_file():
        return {"feeds": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return {"feeds": []}
    data.setdefault("feeds", [])
    return data


def _save_feeds(workspace: Path, data: dict[str, Any]) -> None:
    (workspace / "feeds.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def cmd_subscribe(args: argparse.Namespace) -> None:
    feeds = _load_feeds(args.workspace)
    url = args.url
    category = args.category
    if any(f["url"] == url for f in feeds["feeds"]):
        print(f"Already subscribed to {url}")
        return
    feeds["feeds"].append({"url": url, "category": category, "added": _now_iso(), "last_checked": None})
    _save_feeds(args.workspace, feeds)
    print(f"Subscribed to {url} in category '{category}'")


def cmd_list(args: argparse.Namespace) -> None:
    feeds = _load_feeds(args.workspace)
    selected = [f for f in feeds["feeds"] if args.category in (None, f["category"])]
    if not selected:
        print("No feeds found.")
        return

    fetched_dir = args.workspace / "fetched"
    fetched_dir.mkdir(parents=True, exist_ok=True)
    now = _now_iso()
    results: list[dict[str, str]] = []

    for f in selected:
        items = parse_feed(f["url"])
        last_checked = f.get("last_checked") or ""
        for item in items:
            if last_checked and item["pubDate"] and item["pubDate"] <= last_checked:
                continue
            if (fetched_dir / (_safe_filename(item["title"]) + ".txt")).is_file():
                continue
            tagged = dict(item)
            tagged["feed"] = f["url"]
            results.append(tagged)
        f["last_checked"] = now

    _save_feeds(args.workspace, feeds)
    results = results[:LIST_LIMIT]
    (args.workspace / "last_list.json").write_text(json.dumps(results), encoding="utf-8")

    if not results:
        print("No new posts.")
        return
    for i, item in enumerate(results, 1):
        print(f"{i}. [{item['link']}] {item['title']}")
    print()
    print(f"{len(results)} new post(s) found.")


def _write_fetched(workspace: Path, title: str, link: str, pub: str, desc: str) -> str:
    fetched_dir = workspace / "fetched"
    fetched_dir.mkdir(parents=True, exist_ok=True)
    filename = _safe_filename(title) + ".txt"
    content = f"Title: {title}\nLink:  {link}\nDate:  {pub}\n\n{_strip_html(desc)}\n"
    (fetched_dir / filename).write_text(content, encoding="utf-8")
    return f"fetched/{filename}"


def cmd_fetch(args: argparse.Namespace) -> None:
    query = args.query
    last_list_path = args.workspace / "last_list.json"

    if query.isdigit():
        if not last_list_path.is_file():
            _die(2, "no list results cached — run 'list' first")
        cached = json.loads(last_list_path.read_text(encoding="utf-8"))
        idx = int(query) - 1
        if not (0 <= idx < len(cached)):
            _die(2, f"post number {query} out of range (1-{len(cached)})")
        item = cached[idx]
        feed_items = parse_feed(item["feed"])
        match = next((x for x in feed_items if x["title"] == item["title"]), None)
        desc = (match or {}).get("description", "")
        pub = (match or {}).get("pubDate", "") or item.get("pubDate", "")
        saved = _write_fetched(args.workspace, item["title"], item["link"], pub, desc)
        print(f"Saved: {saved}")
        return

    is_url = bool(re.match(r"^https?://", query))
    if is_url:
        urls = [query]
    else:
        feeds = _load_feeds(args.workspace)
        urls = [f["url"] for f in feeds["feeds"]]
    if not urls:
        _die(2, "no feeds configured.")

    saved_count = 0
    needle = query.lower()
    for url in urls:
        items = parse_feed(url)
        matched = items if is_url else [i for i in items if needle in i["title"].lower()]
        for item in matched:
            path = _write_fetched(args.workspace, item["title"], item["link"], item["pubDate"], item["description"])
            print(f"Saved: {path}")
            saved_count += 1
    if saved_count == 0:
        print(f"No posts matched '{query}'.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="feed_reader.py", description="RSS/Atom/RDF feed reader")
    parser.add_argument("--workspace", "-w", required=True, type=Path, help="Workspace directory")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sub = sub.add_parser("subscribe", help="Subscribe to a feed URL")
    p_sub.add_argument("url")
    p_sub.add_argument("category", nargs="?", default="uncategorized")
    p_sub.set_defaults(func=cmd_subscribe)

    p_list = sub.add_parser("list", help="List new post titles")
    p_list.add_argument("category", nargs="?", default=None)
    p_list.set_defaults(func=cmd_list)

    p_fetch = sub.add_parser("fetch", help="Fetch post content to disk by number, URL, or title substring")
    p_fetch.add_argument("query")
    p_fetch.set_defaults(func=cmd_fetch)

    args = parser.parse_args(argv)
    args.workspace.mkdir(parents=True, exist_ok=True)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
