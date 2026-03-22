# pillbug-a2a

HTTP-based A2A channel plugin for Pillbug, implemented as a uv workspace member.

Enable it from the root application with:

```bash
uv sync --extra a2a
export PB_ENABLED_CHANNELS=cli,a2a
export PB_CHANNEL_PLUGIN_FACTORIES=a2a=pillbug_a2a.a2a_channel:create_channel
export PB_A2A_SELF_BASE_URL=http://runtime-a:8000
export PB_A2A_BEARER_TOKEN=shared-a2a-bearer-token
export PB_A2A_PEERS_JSON='[
  {
    "runtime_id": "runtime-b",
    "base_url": "http://runtime-b:8000"
  }
]'
uv run python -m app
```

Outbound A2A targets use the form `runtime_id/conversation_id`.
For example, `a2a:runtime-b/deploy-42` sends a message to runtime `runtime-b` and keeps the remote conversation under `deploy-42`.

Inbound peer traffic is accepted on `POST /a2a/messages` and requires `Authorization: Bearer <token>` when `PB_A2A_BEARER_TOKEN` is configured.

`PB_A2A_PEERS_JSON` accepts either:

```json
[
  {
    "runtime_id": "runtime-b",
    "base_url": "http://runtime-b:8000",
    "bearer_token": "optional-peer-specific-token"
  }
]
```

or a mapping form:

```json
{
  "runtime-b": "http://runtime-b:8000",
  "runtime-c": {
    "base_url": "http://runtime-c:8000",
    "bearer_token": "optional-peer-specific-token"
  }
}
```

If `PB_A2A_SELF_BASE_URL` is set, the channel includes that base URL in outbound envelope metadata so peers can reply without guessing the sender address.
