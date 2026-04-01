#!/usr/bin/env bash
set -euo pipefail

TEXT="$1"

if [ -n "${2:-}" ]; then
  OUTPUT_FILE="$2"
  mkdir -p "$(dirname "$OUTPUT_FILE")"
else
  OUTPUT_DIR="${ELEVENLABS_OUTPUT_DIR:-/tmp/tts}"
  mkdir -p "$OUTPUT_DIR"
  OUTPUT_FILE="${OUTPUT_DIR}/$(date +%Y%m%d_%H%M%S).ogg"
fi

: "${ELEVENLABS_API_KEY:?ELEVENLABS_API_KEY is not set}"
: "${ELEVENLABS_VOICE_ID:?ELEVENLABS_VOICE_ID is not set}"

MODEL="${ELEVENLABS_MODEL:-eleven_v3}"
STABILITY="${ELEVENLABS_STABILITY:-1.0}"
SIMILARITY="${ELEVENLABS_SIMILARITY:-1.0}"
SPEED="${ELEVENLABS_SPEED:-1.0}"
OUTPUT_FORMAT="${ELEVENLABS_OUTPUT:-opus_48000_192}"

HTTP_CODE=$(curl -s -o "$OUTPUT_FILE" -w "%{http_code}" \
  -X POST "https://api.elevenlabs.io/v1/text-to-speech/${ELEVENLABS_VOICE_ID}?output_format=${OUTPUT_FORMAT}" \
  -H "xi-api-key: ${ELEVENLABS_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
    --arg text "$TEXT" \
    --arg model "$MODEL" \
    --argjson stability "$STABILITY" \
    --argjson similarity "$SIMILARITY" \
    --argjson speed "$SPEED" \
    '{
      text: $text,
      model_id: $model,
      voice_settings: {
        stability: $stability,
        similarity_boost: $similarity,
        speed: $speed
      }
    }'
  )")

if [ "$HTTP_CODE" -ne 200 ]; then
  echo "Error: ElevenLabs API returned HTTP $HTTP_CODE" >&2
  cat "$OUTPUT_FILE" >&2
  rm -f "$OUTPUT_FILE"
  exit 1
fi

echo "$OUTPUT_FILE"
