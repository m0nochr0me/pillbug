#!/usr/bin/env bash
set -euo pipefail

# Credentials resolve from /run/secrets/<name> (Docker/Kubernetes secrets) first;
# for dev, fall back to the environment or a skill-local .env file.
SKILL_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$SKILL_DIR/.env" ]; then
  set -a
  . "$SKILL_DIR/.env"
  set +a
fi

read_secret() {
  local file="/run/secrets/${1,,}"
  [ -s "$file" ] && { cat "$file"; return; }
  printf '%s' "${!1-}"
}

if [ $# -ne 1 ] || [ -z "$1" ]; then
  echo "Usage: bash search.sh <query>" >&2
  exit 1
fi

QUERY="$1"
API_KEY="$(read_secret TAVILY_API_KEY)"
: "${API_KEY:?TAVILY_API_KEY is not set (provide /run/secrets/tavily_api_key, the environment, or skill .env)}"
DEPTH="${TAVILY_DEPTH:-basic}"
TOP_K="${TAVILY_TOP_K:-5}"

if ! [[ "$TOP_K" =~ ^[0-9]+$ ]]; then
  echo "TAVILY_TOP_K must be an integer" >&2
  exit 1
fi

response_file=$(mktemp)
cleanup() {
  rm -f "$response_file"
}
trap cleanup EXIT

http_code=$(curl -sS -o "$response_file" -w "%{http_code}" -X POST https://api.tavily.com/search \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${API_KEY}" \
  -d "$(jq -n \
    --arg q "$QUERY" \
    --arg d "$DEPTH" \
    --argjson k "$TOP_K" \
    '{query: $q, search_depth: $d, max_results: $k}'
  )")

if [ "$http_code" -lt 200 ] || [ "$http_code" -ge 300 ]; then
  echo "Tavily API request failed with HTTP ${http_code}" >&2
  cat "$response_file" >&2
  exit 1
fi

jq '
  def prune:
    walk(
      if type == "object" then
        with_entries(select(.value != null and .value != []))
      elif type == "array" then
        map(select(. != null and . != []))
      else
        .
      end
    );
  prune
' "$response_file"
