## Telegram Channel Memo

The Telegram channel supports sending files from the workspace to users.
Use the `send_file` tool with a `telegram:{{ chat_id_example }}` target to deliver workspace files.

Supported delivery methods (set via the `send_as` parameter):
- `document` — any file type, delivered as a document attachment (default)
- `voice` — audio delivered as a Telegram voice message (best with `.ogg` opus-encoded files)
- `audio` — audio delivered via the music player
- `photo` — image files
- `video` — video files

If `send_as` is omitted, the delivery method is inferred from the file MIME type.
You can send multiple files by calling `send_file` multiple times.
