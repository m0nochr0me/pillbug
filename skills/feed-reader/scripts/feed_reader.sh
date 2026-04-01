#!/usr/bin/env bash
set -euo pipefail

# feed_reader.sh — RSS/Atom feed reader (bash + curl + jq)
# Config and output live under a workspace directory.
#
# Usage:
#   feed_reader.sh --workspace DIR subscribe URL [CATEGORY]
#   feed_reader.sh --workspace DIR list [CATEGORY]
#   feed_reader.sh --workspace DIR fetch URL|TITLE

WORKSPACE=""
CMD=""
ARGS=()

# ── parse args ───────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace|-w) WORKSPACE="$2"; shift 2 ;;
    *)
      if [[ -z "$CMD" ]]; then CMD="$1"; shift
      else ARGS+=("$1"); shift
      fi
      ;;
  esac
done

if [[ -z "$WORKSPACE" ]]; then
  echo "error: --workspace DIR is required" >&2; exit 1
fi
if [[ -z "$CMD" ]]; then
  echo "error: command required (subscribe|list|fetch)" >&2; exit 1
fi

FEEDS_FILE="$WORKSPACE/feeds.json"
FETCHED_DIR="$WORKSPACE/fetched"
LAST_LIST="$WORKSPACE/last_list.json"
mkdir -p "$FETCHED_DIR"

# ensure config exists
if [[ ! -f "$FEEDS_FILE" ]]; then
  echo '{"feeds":[]}' > "$FEEDS_FILE"
fi

# ── helpers ──────────────────────────────────────────────────────────────

now_iso() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

# Fetch a feed URL and emit JSON array of items: [{title, link, pubDate, description}]
parse_feed() {
  local url="$1"
  local raw
  raw=$(curl -sL --max-time 30 -H "User-Agent: feed-reader/1.0" "$url") || { echo "[]"; return; }

  # Detect Atom vs RSS
  if echo "$raw" | grep -q '<feed'; then
    # Atom
    echo "$raw" | python3 -c "
import sys, xml.etree.ElementTree as ET, json, html
ns = {'a': 'http://www.w3.org/2005/Atom'}
tree = ET.parse(sys.stdin)
items = []
for e in tree.findall('.//a:entry', ns):
    title = e.findtext('a:title', '', ns).strip()
    link_el = e.find('a:link[@rel=\"alternate\"]', ns)
    if link_el is None:
        link_el = e.find('a:link', ns)
    link = link_el.get('href', '') if link_el is not None else ''
    pub = e.findtext('a:updated', '', ns) or e.findtext('a:published', '', ns)
    desc = e.findtext('a:summary', '', ns) or e.findtext('a:content', '', ns) or ''
    items.append({'title': title, 'link': link, 'pubDate': pub, 'description': desc})
print(json.dumps(items))
" 2>/dev/null || echo "[]"
  else
    # RSS
    echo "$raw" | python3 -c "
import sys, xml.etree.ElementTree as ET, json
ns = {'content': 'http://purl.org/rss/1.0/modules/content/',
      'dc': 'http://purl.org/dc/elements/1.1/'}
tree = ET.parse(sys.stdin)
items = []
for item in tree.iter('item'):
    title = (item.findtext('title') or '').strip()
    link = (item.findtext('link') or '').strip()
    pub = item.findtext('pubDate') or item.findtext('dc:date', namespaces=ns) or ''
    desc = item.findtext('content:encoded', namespaces=ns) or item.findtext('description') or ''
    items.append({'title': title, 'link': link, 'pubDate': pub, 'description': desc})
print(json.dumps(items))
" 2>/dev/null || echo "[]"
  fi
}

# Sanitize a string for use as a filename
safe_filename() {
  echo "$1" | sed 's/[^a-zA-Z0-9._-]/_/g' | head -c 200
}

# ── commands ─────────────────────────────────────────────────────────────

cmd_subscribe() {
  local url="${ARGS[0]:-}"
  local category="${ARGS[1]:-uncategorized}"
  if [[ -z "$url" ]]; then
    echo "error: subscribe requires a feed URL" >&2; exit 1
  fi

  # Check for duplicate
  local existing
  existing=$(jq -r --arg u "$url" '.feeds[] | select(.url == $u) | .url' "$FEEDS_FILE")
  if [[ -n "$existing" ]]; then
    echo "Already subscribed to $url"
    return
  fi

  local tmp
  tmp=$(mktemp)
  jq --arg u "$url" --arg c "$category" --arg t "$(now_iso)" \
    '.feeds += [{"url": $u, "category": $c, "added": $t, "last_checked": null}]' \
    "$FEEDS_FILE" > "$tmp" && mv "$tmp" "$FEEDS_FILE"

  echo "Subscribed to $url in category '$category'"
}

cmd_list() {
  local category="${ARGS[0]:-}"

  # Collect feeds to check
  local urls
  if [[ -n "$category" ]]; then
    urls=$(jq -r --arg c "$category" '.feeds[] | select(.category == $c) | .url' "$FEEDS_FILE")
  else
    urls=$(jq -r '.feeds[].url' "$FEEDS_FILE")
  fi

  if [[ -z "$urls" ]]; then
    echo "No feeds found."
    return
  fi

  local now
  now=$(now_iso)
  local results="[]"

  while IFS= read -r url; do
    local last_checked
    last_checked=$(jq -r --arg u "$url" '.feeds[] | select(.url == $u) | .last_checked // ""' "$FEEDS_FILE")

    local items
    items=$(parse_feed "$url")

    # Filter to items newer than last_checked (if set)
    if [[ -n "$last_checked" ]]; then
      items=$(echo "$items" | jq --arg lc "$last_checked" '[.[] | select(.pubDate == "" or .pubDate > $lc)]')
    fi

    # Tag each item with feed url
    items=$(echo "$items" | jq --arg u "$url" '[.[] | . + {"feed": $u}]')
    results=$(echo "$results" "$items" | jq -s '.[0] + .[1]')

    # Update last_checked
    local tmp
    tmp=$(mktemp)
    jq --arg u "$url" --arg t "$now" \
      '(.feeds[] | select(.url == $u)).last_checked = $t' \
      "$FEEDS_FILE" > "$tmp" && mv "$tmp" "$FEEDS_FILE"
  done <<< "$urls"

  # Exclude already-fetched posts (match by safe_filename)
  local filtered="[]"
  local total
  total=$(echo "$results" | jq 'length')
  local i=0
  while [[ $i -lt $total ]]; do
    local title
    title=$(echo "$results" | jq -r ".[$i].title")
    local fname
    fname="$(safe_filename "$title").txt"
    if [[ ! -f "$FETCHED_DIR/$fname" ]]; then
      filtered=$(echo "$filtered" | jq --argjson item "$(echo "$results" | jq ".[$i]")" '. + [$item]')
    fi
    i=$((i + 1))
  done
  results="$filtered"

  # Truncate to 16 items
  results=$(echo "$results" | jq '.[0:16]')

  # Save for fetch-by-number
  echo "$results" > "$LAST_LIST"

  # Output numbered titles
  local count
  count=$(echo "$results" | jq 'length')
  if [[ "$count" == "0" ]]; then
    echo "No new posts."
  else
    echo "$results" | jq -r 'to_entries[] | "\(.key + 1). [\(.value.feed)] \(.value.title)"'
    echo ""
    echo "$count new post(s) found."
  fi
}

fetch_item() {
  local title="$1" link="$2" desc="$3" pub="$4"
  local filename
  filename="$(safe_filename "$title").txt"
  {
    echo "Title: $title"
    echo "Link:  $link"
    echo "Date:  $pub"
    echo ""
    echo "$desc" | python3 -c "
import sys, html, re
raw = sys.stdin.read()
text = html.unescape(raw)
text = re.sub(r'<[^>]+>', '', text)
print(text.strip())
"
  } > "$FETCHED_DIR/$filename"
  echo "Saved: fetched/$filename"
}

cmd_fetch() {
  local query="${ARGS[0]:-}"
  if [[ -z "$query" ]]; then
    echo "error: fetch requires a post number, feed URL, or title substring" >&2; exit 1
  fi

  # Fetch by post number from last list results
  if [[ "$query" =~ ^[0-9]+$ ]]; then
    if [[ ! -f "$LAST_LIST" ]]; then
      echo "error: no list results cached — run 'list' first" >&2; exit 1
    fi
    local idx=$(( query - 1 ))
    local total
    total=$(jq 'length' "$LAST_LIST")
    if [[ $idx -lt 0 || $idx -ge $total ]]; then
      echo "error: post number $query out of range (1-$total)" >&2; exit 1
    fi
    local item
    item=$(jq ".[$idx]" "$LAST_LIST")
    local title link feed_url
    title=$(echo "$item" | jq -r '.title')
    link=$(echo "$item" | jq -r '.link')
    feed_url=$(echo "$item" | jq -r '.feed')

    # Re-fetch the feed to get full description (list results may lack it)
    local items
    items=$(parse_feed "$feed_url")
    local desc pub
    desc=$(echo "$items" | jq -r --arg t "$title" '[.[] | select(.title == $t)][0].description // ""')
    pub=$(echo "$items" | jq -r --arg t "$title" '[.[] | select(.title == $t)][0].pubDate // ""')

    fetch_item "$title" "$link" "$desc" "$pub"
    return
  fi

  # Determine which feeds to search
  local urls
  if [[ "$query" =~ ^https?:// ]]; then
    urls="$query"
  else
    urls=$(jq -r '.feeds[].url' "$FEEDS_FILE")
  fi

  if [[ -z "$urls" ]]; then
    echo "No feeds configured." >&2; exit 1
  fi

  local saved_count=0
  while IFS= read -r url; do
    local items
    items=$(parse_feed "$url")

    local matched
    if [[ "$query" =~ ^https?:// ]]; then
      matched="$items"
    else
      matched=$(echo "$items" | jq --arg q "$query" '[.[] | select(.title | ascii_downcase | contains($q | ascii_downcase))]')
    fi

    local count
    count=$(echo "$matched" | jq 'length')
    if [[ "$count" == "0" ]]; then continue; fi

    local i=0
    while [[ $i -lt $count ]]; do
      local title link desc pub
      title=$(echo "$matched" | jq -r ".[$i].title")
      link=$(echo "$matched" | jq -r ".[$i].link")
      desc=$(echo "$matched" | jq -r ".[$i].description")
      pub=$(echo "$matched" | jq -r ".[$i].pubDate")

      fetch_item "$title" "$link" "$desc" "$pub"
      saved_count=$((saved_count + 1))
      i=$((i + 1))
    done
  done <<< "$urls"

  if [[ "$saved_count" == "0" ]]; then
    echo "No posts matched '$query'."
  fi
}

# ── dispatch ─────────────────────────────────────────────────────────────

case "$CMD" in
  subscribe) cmd_subscribe ;;
  list)      cmd_list ;;
  fetch)     cmd_fetch ;;
  *)
    echo "Unknown command: $CMD" >&2
    echo "Usage: feed_reader.sh --workspace DIR (subscribe|list|fetch) [args...]" >&2
    exit 1
    ;;
esac
