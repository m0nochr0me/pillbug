---
name: text-to-speech
description: Synthesize speech from text using ElevenLabs TTS API. Use when the user asks to speak, say, pronounce, vocalize, read aloud, or generate audio from text.
---

# Text-to-Speech

Synthesize speech audio from text using the ElevenLabs API. Voice, model, and synthesis settings are pre-configured via environment variables.

## Usage

Run the synthesis script with the text to speak as argument:

```bash
bash skills/text-to-speech/scripts/synthesize.sh "Text to synthesize" [output_file]
```

The optional second argument specifies the output file path. When omitted, the file is saved to `$ELEVENLABS_OUTPUT_DIR` (or `/tmp/tts/`) with a timestamp-based name. The script prints the output file path on success.

## Examples

Text can include special voice or style tags in square brackets to modify the synthesis (e.g. `[mischievously]`, `[calmly]`, `[excitedly]`, `[sighs]`, `[exhales]` etc.):

User: "Say hello in a mischievously voice"
```bash
bash skills/text-to-speech/scripts/synthesize.sh "[mischievously] Hello!"
```

User: "Read this aloud: The quick brown fox jumps over the lazy dog"
```bash
bash skills/text-to-speech/scripts/synthesize.sh "The quick brown fox jumps over the lazy dog"
```

## Notes
- Requires `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` environment variables.
- Requires `jq` and `curl` on the system.
- Output files are timestamped to avoid collisions.
