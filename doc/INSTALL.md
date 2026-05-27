# Installation Instructions

## For Humans

Pillbug is easiest to install on Linux and macOS with Docker.

Choose one of these variants:

- Simple local setup: one Pillbug runtime on your machine, usually for CLI use plus optional Telegram or trigger integrations.
- Multi-agent setup: two or more Pillbug runtimes plus Redis, optional Arca-Memory, and optional dashboard services.

The first launch is expected to stop intentionally.
That bootstrap run creates the runtime directory structure and seeds the initial workspace file.
Treat that first stop as a normal setup step, not as a failure.

### Simpliest local setup (not recommended for production)

Pillbug targets Python 3.14+ and uses `uv` for dependency management.

```bash
uv sync --locked
export PB_GEMINI_API_KEY=your_api_key
./run.sh
```

Alternative launch commands:

```bash
uv run python -m app
uv run python -m app.mcp
```

Optional packages are installed through uv extras:

```bash
uv sync --extra a2a
uv sync --extra telegram
uv sync --extra matrix
uv sync --extra websocket
uv sync --extra trigger
uv sync --extra dashboard
uv sync --extra genai_proxy
uv sync --extra claude_api_proxy
```

Combine extras when more than one integration is needed, for example:

```bash
uv sync --extra telegram,matrix,trigger,websocket
```

The `genai_proxy` extra installs the `pillbug-genai-proxy` console script, a FastAPI translator that exposes the Gemini wire format on top of any OpenAI-compatible chat completions endpoint (llama.cpp, vLLM, LiteLLM, Ollama). Once it is running, point the Pillbug runtime at it with `PB_GEMINI_BASE_URL` instead of hitting the Google Gemini API. See [packages/pillbug-genai-proxy/README.md](../packages/pillbug-genai-proxy/README.md).

The `claude_api_proxy` extra installs the sibling `pillbug-claude-api-proxy` console script. Same Gemini wire-format entry, but upstream calls go through the official [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python) and are **billed against your Claude Pro/Max subscription** via the Claude Code OAuth token. Mint a token once with `claude setup-token` and export it as `CLAUDE_CODE_OAUTH_TOKEN` (or `PB_CLAUDE_API_PROXY_OAUTH_TOKEN`) on the proxy host. Point Pillbug at the proxy by setting `PB_GEMINI_BASE_URL`. See [packages/pillbug-claude-api-proxy/README.md](../packages/pillbug-claude-api-proxy/README.md).

## For Agents

Ask the user which setup variant they want before making changes:

- `simple local`
- `multi-agent`

Then follow this workflow:

1. Clone Pillbug and, if external memory is requested, clone Arca-Memory from its Git repository.
2. Copy the matching example files from `doc/simple/` or `doc/multi/` into a writable working directory.
3. Replace every placeholder secret and identifier before starting containers.
4. Run the bootstrap launch once.
5. Wait for the runtime to exit after printing that the workspace was initialized.
6. Verify that the runtime directory now contains at least `runtime_id.txt`, `security_patterns.json`, and `workspace/AGENTS.md`.
7. If the runtime should expose bundled workspace skills, copy the repository `skills/` directory into `<runtime-base>/workspace/skills/` after the bootstrap run. If any of those skills call third-party APIs, set up their credentials per [Skill Secrets](#skill-secrets).
8. Populate the remaining editable config files. Copy `doc/common/example_mcp.json` to `<runtime-base>/mcp.json`. If the trigger channel is enabled, copy `doc/common/example_trigger_sources.json` to `<runtime-base>/trigger_sources.json`. If trigger is disabled, create `<runtime-base>/trigger_sources.json` with `[]`.
9. Edit `AGENTS.md`, `mcp.json`, and `trigger_sources.json` for the target runtime.
10. Start the runtime again and confirm that the HTTP and channel endpoints stay up.

Agent success criteria:

- bootstrap launch exits cleanly after creating the workspace
- runtime base directory contains the expected files
- second launch stays running
- `GET /health` responds when the HTTP server is enabled

## Prerequisites

- Linux host: Docker Engine with the Compose plugin
- macOS host: Docker Desktop
- Docker Compose: use `docker compose`
- Git: [Install Git](https://git-scm.com/downloads)
- A Gemini API key for `PB_GEMINI_API_KEY`

### Recommended

- `curl` for health checks and local API tests
- `jq` for inspecting JSON responses and editing copied example files
- `uv` if you also want to run Pillbug directly on the host outside Docker

Optional external memory service:

- Arca-Memory: [Arca Memory](https://github.com/m0nochr0me/arca-mcp)
- There is no official Docker image yet. Build it locally from the Git repository.

```bash
git clone https://github.com/m0nochr0me/pillbug.git
git clone https://github.com/m0nochr0me/arca-mcp.git
cp pillbug/doc/multi/example_arca.env arca-mcp/.arca.env
cd arca-mcp

docker build -t arca-memory:latest .

# Example standalone launch
docker run -d \
  --name arca-memory \
  --env-file ./.arca.env \
  -p 8201:8201 \
  -v "$HOME/.arca-memory:/var/lib/arca-memory" \
  arca-memory:latest
```

## Variants

### Simple Local Setup

Use this when you want one runtime on one machine.

1. Clone Pillbug.

```bash
git clone https://github.com/m0nochr0me/pillbug.git
cd pillbug
```

1. Copy the single-runtime example env file.

```bash
cp doc/simple/example_runtime.env ./.runtime.env
mkdir -p "$HOME/.pillbug/local"
```

1. Edit `./.runtime.env`.

Required changes:

- set `PB_GEMINI_API_KEY`
- choose a unique `PB_RUNTIME_ID`
- choose an agent name in `PB_AGENT_NAME`
- replace every bearer token and third-party API placeholder you intend to use

For the smallest local install, change these values before building:

- set `PB_ENABLED_CHANNELS=cli`
- set `PB_CHANNEL_PLUGIN_FACTORIES=`
- remove or ignore Telegram, trigger, ElevenLabs, and Tavily settings until you need them

If you keep `telegram` or `trigger` enabled, build with the matching extras.

1. Build the image.

CLI-only build:

```bash
docker build -t pillbug-local:latest .
```

Build with Telegram and trigger support:

```bash
docker build \
  --build-arg PILLBUG_INSTALL_EXTRAS=telegram,trigger \
  --build-arg EXTRA_PACKAGES="ca-certificates curl jq" \
  -t pillbug-local:latest \
  .
```

Build with Matrix, WebSocket, and trigger support:

```bash
docker build \
  --build-arg PILLBUG_INSTALL_EXTRAS=matrix,websocket,trigger \
  --build-arg EXTRA_PACKAGES="ca-certificates curl jq" \
  -t pillbug-local:latest \
  .
```

For Matrix you must obtain an access token once before starting the runtime. With the matrix extra installed, run `uv run pillbug-matrix-access-token --homeserver https://matrix.example.org --user-id @pillbug:example.org` and copy the printed `PB_MATRIX_*` exports into your env file. See [packages/pillbug-matrix/README.md](../packages/pillbug-matrix/README.md).

1. Run the bootstrap launch once.

```bash
docker run --rm -it \
  --name pillbug-bootstrap \
  --env-file ./.runtime.env \
  -e PB_BASE_DIR=/home/pillbug \
  -p 8000:8000 \
  -p 9100:9100 \
  -v "$HOME/.pillbug/local:/home/pillbug" \
  pillbug-local:latest
```

Notes:

- On Linux, if the runtime needs to reach services on the host, add `--add-host host.docker.internal:host-gateway`.
- On macOS, Docker Desktop already provides `host.docker.internal`.
- The container is expected to exit after creating `workspace/AGENTS.md`.

1. Populate the editable runtime files.

```bash
mkdir -p "$HOME/.pillbug/local/workspace/skills"
cp doc/common/example_mcp.json "$HOME/.pillbug/local/mcp.json"
cp doc/common/example_trigger_sources.json "$HOME/.pillbug/local/trigger_sources.json"
cp -R skills/. "$HOME/.pillbug/local/workspace/skills/"
```

Then edit:

- `$HOME/.pillbug/local/workspace/AGENTS.md`
- `$HOME/.pillbug/local/workspace/skills/`
- `$HOME/.pillbug/local/mcp.json`
- `$HOME/.pillbug/local/trigger_sources.json`

Pillbug only auto-discovers custom skills from `workspace/skills/*/SKILL.md`, so copy them after the first launch has created the workspace.

If you are not using trigger events yet, replace `trigger_sources.json` with:

```json
[]
```

If you are not using Arca-Memory yet, either omit `mcp.json` or reduce it to an empty config:

```json
{
  "servers": {},
  "inputs": []
}
```

1. Start the real runtime.

```bash
docker run --rm -it \
  --name pillbug-local \
  --env-file ./.runtime.env \
  -e PB_BASE_DIR=/home/pillbug \
  -p 8000:8000 \
  -p 9100:9100 \
  -v "$HOME/.pillbug/local:/home/pillbug" \
  pillbug-local:latest
```

1. Verify the install.

```bash
curl http://127.0.0.1:8000/health
```

### Multi-Agent Setup

Use this when you want a fleet of Pillbug runtimes that can talk to each other over A2A. The example stack pairs three differently-shaped agents — a Gemini Pro generalist on Matrix, a cron-worker driven by a local model via the bundled `genai-proxy`, and a Flash assistant on Telegram — with a Redis used by the embedded Docket scheduler and an optional operator dashboard.

1. Clone Pillbug.

```bash
git clone https://github.com/m0nochr0me/pillbug.git
cd pillbug
```

1. (Optional) Build the local Arca-Memory image.

The default example stack does not include arca-memory. Skip this step unless you intend to uncomment the arca block at the bottom of the compose file and share a graph + semantic memory MCP across the fleet.

```bash
cd ..
git clone https://github.com/m0nochr0me/arca-mcp.git
cd arca-mcp
docker build -t arca-memory:latest .
cd ../pillbug
```

1. Copy the example files into a local working directory.

```bash
mkdir -p ./.deploy-multi
cp doc/multi/example.compose.yaml ./.deploy-multi/compose.yaml
cp doc/multi/example_generalist.env  ./.deploy-multi/example_generalist.env
cp doc/multi/example_cron-worker.env ./.deploy-multi/example_cron-worker.env
cp doc/multi/example_assistant.env   ./.deploy-multi/example_assistant.env
cp doc/multi/example_genai-proxy.env ./.deploy-multi/example_genai-proxy.env
cp doc/multi/example_dashboard.env   ./.deploy-multi/example_dashboard.env
# Only if you uncommented the arca-memory block in the compose file:
# cp doc/multi/example_arca.env       ./.deploy-multi/example_arca.env
mkdir -p "$HOME/.pillbug/generalist" "$HOME/.pillbug/cron-worker" "$HOME/.pillbug/assistant" "$HOME/.pillbug/dashboard"
```

1. Edit the copied env files.

Required changes:

- set `PB_GEMINI_API_KEY` on `generalist` and `assistant` (the cron-worker talks to the proxy and ignores its API key)
- in `example_genai-proxy.env`, point `PB_GENAI_PROXY_UPSTREAM_URL` at your local model server (llama.cpp / vLLM / Ollama / LiteLLM). The default assumes a host-local OpenAI-compatible endpoint reachable via `http://host.docker.internal:8080/v1`. Adjust `PB_GEMINI_MODEL` in `example_cron-worker.env` to a model id your upstream actually serves.
- replace dashboard, A2A, and trigger bearer tokens
- fill in real Telegram/Matrix tokens and allowed chat/room IDs (or remove those channels from `PB_ENABLED_CHANNELS` if you don't use them)
- (optional) for arca-memory: set `ARCA_REDIS_HOST=redis` and the auth/API values in `example_arca.env`

1. Bootstrap the runtimes once.

```bash
docker compose -f ./.deploy-multi/compose.yaml up --build generalist cron-worker assistant
```

Expected result:

- each Pillbug runtime exits once after seeding its workspace
- `genai-proxy` and `redis` may continue running

1. Populate the runtime-local editable files.

```bash
for r in generalist cron-worker assistant; do
  mkdir -p "$HOME/.pillbug/$r/workspace/skills"
  cp doc/common/example_mcp.json "$HOME/.pillbug/$r/mcp.json"
  cp -R skills/. "$HOME/.pillbug/$r/workspace/skills/"
done
cp doc/common/example_trigger_sources.json "$HOME/.pillbug/cron-worker/trigger_sources.json"
```

Then edit each runtime's `workspace/AGENTS.md`, `workspace/skills/`, `mcp.json`, and (for cron-worker) `trigger_sources.json` to suit the role.

When arca-memory runs inside the same compose stack, point `mcp.json` at `http://arca-memory:8201/app/mcp`.
When Pillbug runs alone in Docker but Arca is published on the host, use `http://host.docker.internal:8201/app/mcp` instead.

1. Start the full stack.

```bash
docker compose -f ./.deploy-multi/compose.yaml up -d --build
```

1. Verify the services.

```bash
curl http://127.0.0.1:8001/health   # generalist
curl http://127.0.0.1:8002/health   # cron-worker
curl http://127.0.0.1:8003/health   # assistant
curl http://127.0.0.1:8010/         # dashboard
```

## First-Time Setup

The first launch creates the runtime directory structure under `PB_BASE_DIR` and then exits intentionally.

After that bootstrap pass, make sure each runtime base directory contains these files:

- `runtime_id.txt`
- `security_patterns.json`
- `workspace/AGENTS.md`
- `workspace/skills/` if you want bundled custom skills available in that runtime
- `workspace/plans/active/` (empty; populated when the model enters planning mode)
- `workspace/inbox/<channel>/` for each channel that delivers attachments (defaults: `cli`, `telegram`, `a2a`)
- `workspace/fetched/` (created on first `fetch_url`; artifacts carry a `trust: untrusted` banner)
- `mcp.json`
- `trigger_sources.json`

What creates them:

- `runtime_id.txt`: created automatically on first launch unless `PB_RUNTIME_ID` is already set
- `security_patterns.json`: created automatically on first launch
- `workspace/AGENTS.md`: created automatically on first launch, then the process exits
- `workspace/skills/`: copy from the repository `skills/` directory after the workspace exists; Pillbug discovers custom skills from `workspace/skills/*/SKILL.md`
- `workspace/plans/active/` and `workspace/inbox/<channel>/`: ensured by `workspace_init()` on every launch
- `mcp.json`: copy from `doc/common/example_mcp.json` and edit it for your environment
- `trigger_sources.json`: created as `[]` by the trigger plugin when that channel is enabled, or copy from `doc/common/example_trigger_sources.json` to pre-seed real rules

Runtime state outside the workspace (auto-created under `PB_BASE_DIR` as needed):

- `approvals/<id>.json` — command-draft approval records produced by `draft_command`.
- `drafts/<id>.json` — outbound-draft records produced by `draft_outbound_message` and any `send_*` call against a non-autosend channel.
- `tasks/agent_tasks.json` — scheduled-task registry (when not using a Redis-backed Docket).
- `tasks/<task_id>/progress.jsonl` — per-task run log when the task carries a `goal` record.

Minimum post-bootstrap edits:

1. Review `workspace/AGENTS.md` and replace the default persona with your real agent instructions.
2. Copy the repository `skills/` directory into `workspace/skills/` if you want the bundled skills available in that runtime.
3. Edit `mcp.json` so external MCP servers point at reachable URLs and use real bearer tokens and namespaces.
4. Edit `trigger_sources.json` so each trigger source has the prompt and urgency rules you want.
5. Restart the runtime.

Useful checks:

```bash
ls -la "$HOME/.pillbug/local"
ls -la "$HOME/.pillbug/local/workspace"
curl http://127.0.0.1:8000/health
```

For multi-agent installs, repeat the same checks for each runtime directory such as `~/.pillbug/runtime-a` and `~/.pillbug/runtime-b`.

## Skill Secrets

Workspace skills that call third-party APIs (for example `text-to-speech` and `tavily-search`) need credentials. Skill helper scripts run inside the `execute_command` sandbox, which strips secret-looking variables from the environment, so a skill's secrets must be supplied as files — not through the runtime env file. See [Skill Secrets in the Configuration Reference](./CONFIGURATION.md#skill-secrets) for the full resolution order.

Each secret is a file at `/run/secrets/<name>`, where `<name>` is the lowercased variable name — for example `TAVILY_API_KEY` → `/run/secrets/tavily_api_key` and `ELEVENLABS_API_KEY` → `/run/secrets/elevenlabs_api_key`.

### Simple local setup

Bind-mount each secret file when starting the container:

```bash
docker run --rm -it \
  --name pillbug-local \
  --env-file ./.runtime.env \
  -e PB_BASE_DIR=/home/pillbug \
  -p 8000:8000 \
  -v "$HOME/.pillbug/local:/home/pillbug" \
  -v "$HOME/.pillbug/secrets/tavily_api_key:/run/secrets/tavily_api_key:ro" \
  -v "$HOME/.pillbug/secrets/elevenlabs_api_key:/run/secrets/elevenlabs_api_key:ro" \
  pillbug-local:latest
```

For development you can instead drop a `.env` file into the skill directory itself (for example `workspace/skills/tavily-search/.env`); it is gitignored and read only when no `/run/secrets` file is present.

### Multi-agent setup

Use Docker Compose secrets. Create the secret files:

```bash
mkdir -p ./.deploy-multi/secrets
printf '%s' 'REAL_TAVILY_API_KEY'     > ./.deploy-multi/secrets/tavily_api_key
printf '%s' 'REAL_ELEVENLABS_API_KEY' > ./.deploy-multi/secrets/elevenlabs_api_key
chmod 0440 ./.deploy-multi/secrets/*
```

`doc/multi/example.compose.yaml` carries a commented `secrets:` block — uncomment the top-level definitions and the per-service `secrets:` list on each runtime that runs those skills. The container runs as uid 1000 (`pillbug`), and each secret file must be readable by that uid: non-Swarm Compose ignores a secret's `uid`/`gid`/`mode`, so set host-side ownership or permissions accordingly.

Non-secret skill settings (voice ids, search depth, and similar) stay in the runtime env file, and their names must be listed in `PB_EXECUTE_COMMAND_ENV_PASSTHROUGH`; the example env files show this.
