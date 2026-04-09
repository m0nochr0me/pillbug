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
| `PB_MATRIX_REPLY_TO_MESSAGE` | no | `true` | Adds `m.in_reply_to` metadata when replying to inbound messages. |

## Runtime behavior

The channel currently behaves as follows:

- It performs an initial sync on startup to capture the current `next_batch` token and skip historical room history.
- It ignores messages sent by its own Matrix user ID.
- It accepts text events plus image, audio, video, and generic file message events.
- Inbound attachments are downloaded into `downloads/matrix/<sanitized-room-id>/` inside the Pillbug workspace.
- Downloaded attachments are exposed through generic `inbound_attachments` metadata so supported Gemini multimodal files can be forwarded without Matrix-specific handling in the AI layer.
- Outbound attachments are uploaded through the Matrix content repository and sent as `m.image`, `m.audio`, `m.video`, or `m.file` based on the attachment metadata or MIME type.
- While Pillbug is generating a response, the plugin sends Matrix typing notifications.
- Long outbound replies are chunked into 4000-character messages for readability.

## Notes

This package currently uses `matrix-nio` without end-to-end encryption support enabled.
If you need E2EE later, add it as a follow-up change instead of assuming encrypted room support is already wired into the plugin.
