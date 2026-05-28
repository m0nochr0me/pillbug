# pillbug-matrix

Matrix channel plugin for Pillbug, implemented as a uv workspace member.

## Install and enable

Install the optional dependency from the repo root:

```bash
uv sync --extra matrix
```

Enable the channel in the root application:

```bash
export PB_ENABLED_CHANNELS=cli,matrix
export PB_CHANNEL_PLUGIN_FACTORIES=matrix=pillbug_matrix.matrix_channel:create_channel
export PB_MATRIX_HOMESERVER_URL=https://matrix.example.org
export PB_MATRIX_USER_ID=@pillbug:example.org
export PB_MATRIX_ACCESS_TOKEN=syt_your_access_token
uv run python -m app
```

If `PB_MATRIX_ALLOWED_ROOM_IDS` is set, the plugin only accepts inbound events from those room IDs.
Messages from other rooms are ignored.

## Get an access token

Pillbug expects a pre-obtained Matrix access token instead of logging in with a password at runtime.
Use the helper command once to perform a password login and print the values you need for `PB_MATRIX_*` settings:

```bash
uv run pillbug-matrix-access-token --homeserver https://matrix.example.org --user-id @pillbug:example.org
```

The command prompts for the password and prints shell exports like this:

```bash
export PB_MATRIX_HOMESERVER_URL='https://matrix.example.org'
export PB_MATRIX_USER_ID='@pillbug:example.org'
export PB_MATRIX_DEVICE_ID='ABCDEF1234'
export PB_MATRIX_ACCESS_TOKEN='syt_...'
```

Add `--format json` if you want JSON output instead of shell exports.

## Configuration

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `PB_MATRIX_HOMESERVER_URL` | yes | none | Matrix homeserver URL. |
| `PB_MATRIX_ACCESS_TOKEN` | yes | none | Access token used by the runtime client. |
| `PB_MATRIX_USER_ID` | yes | none | Full Matrix user ID, for example `@pillbug:example.org`. |
| `PB_MATRIX_DEVICE_ID` | no | unset | Optional device ID to reuse for the runtime client. |
| `PB_MATRIX_ALLOWED_ROOM_IDS` | no | unset | Comma-separated allowlist of room IDs. |
| `PB_MATRIX_SYNC_TIMEOUT_MS` | no | `30000` | `/sync` long-poll timeout in milliseconds. |
| `PB_MATRIX_REPLY_TO_MESSAGE` | no | `true` | Adds `m.in_reply_to` metadata when replying to inbound messages (non-threaded path only). |
| `PB_MATRIX_REPLY_IN_THREAD` | no | `false` | When enabled, the agent treats each Matrix thread as its own Pillbug session and replies inside that thread. Top-level (non-threaded) messages start a fresh thread rooted at the inbound event, so each thread becomes a separate LLM history. Useful for shared rooms; for 1:1 rooms leave disabled. |
| `PB_MATRIX_REACTION_PRESENCE` | no | `false` | When enabled, the presence indicator is a 🤔 reaction added to the inbound user message while Pillbug generates a response (and redacted once the reply is sent) instead of a typing notification. Useful for clients that do not render typing notifications. |

## Runtime behavior

The channel currently behaves as follows:

- It performs an initial sync on startup to capture the current `next_batch` token and skip historical room history.
- It ignores messages sent by its own Matrix user ID.
- It accepts text events plus image, audio, video, and generic file message events.
- Inbound attachments are downloaded into `downloads/matrix/<sanitized-room-id>/` inside the Pillbug workspace.
- Downloaded attachments are exposed through generic `inbound_attachments` metadata so supported Gemini multimodal files can be forwarded without Matrix-specific handling in the AI layer.
- Outbound attachments are uploaded through the Matrix content repository and sent as `m.image`, `m.audio`, `m.video`, or `m.file` based on the attachment metadata or MIME type.
- Outbound `.ogg` files are always sent as `m.audio` with the MSC3245 voice-message markers (`org.matrix.msc3245.voice`, `org.matrix.msc1767.audio`, `org.matrix.msc1767.file`, `org.matrix.msc1767.text`) and the audio duration in milliseconds so clients like Element (web and mobile) render them as voice messages.
- While Pillbug is generating a response, the plugin sends Matrix typing notifications. When `PB_MATRIX_REACTION_PRESENCE` is enabled it instead adds a 🤔 reaction to the inbound user message and redacts that reaction once the reply is sent.
- Long outbound replies are chunked into 4000-character messages for readability.
- When `PB_MATRIX_REPLY_IN_THREAD` is enabled the conversation id becomes `<room_id>:<thread_root_event_id>` so each Matrix thread maps to a distinct Pillbug session, and outbound messages carry an `m.relates_to` block with `rel_type: m.thread` plus a falling-back `m.in_reply_to` so Matrix clients that do not understand threads still render the reply as a normal reply.
- Outbound text is sent as both a plaintext `body` (CommonMark source) and an HTML `formatted_body` rendered with `markdown-it-py` (`org.matrix.custom.html`), so Markdown features such as bold, italics, code blocks, lists, and links render in Matrix clients that support HTML. Chunking happens on the raw Markdown before rendering, so a single fenced block split across chunks may render with a partially closed code block in the affected chunks.

## Notes

This package currently uses `matrix-nio` without end-to-end encryption support enabled.
If you need E2EE later, add it as a follow-up change instead of assuming encrypted room support is already wired into the plugin.
