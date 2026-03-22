# pillbug-a2a

HTTP-based A2A channel plugin for Pillbug, implemented as a uv workspace member.

Enable it from the root application with:

```bash
uv sync --extra a2a
export PB_ENABLED_CHANNELS=cli,a2a
export PB_CHANNEL_PLUGIN_FACTORIES=a2a=pillbug_a2a.a2a_channel:create_channel
export PB_A2A_SELF_BASE_URL=http://runtime-a:8000
export PB_A2A_BEARER_TOKEN=shared-a2a-bearer-token
export PB_A2A_CONVERGENCE_MAX_HOPS=6
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
Automatic follow-up replies are bounded by `PB_A2A_CONVERGENCE_MAX_HOPS`. Terminal intents such as `result`, `inform`, `error`, and `heartbeat` are processed locally without triggering another automatic outbound reply.

Agent discovery is published at `GET /.well-known/agent-card.json`.
When bearer auth is enabled, Pillbug also exposes `GET /extendedAgentCard` with the same token and includes the convergence policy as an Agent Card extension.

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
If Agent Card discovery is enabled, outbound metadata also includes the corresponding Agent Card URL so peers can fetch the runtime's advertised communication profile.
