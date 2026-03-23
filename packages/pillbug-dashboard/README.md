# pillbug-dashboard

FastAPI-based operator dashboard for Pillbug runtimes.

This package stays server-rendered first. It provides the dashboard backend, HTML templates,
local registry persistence, and browser-side polling or event subscription without requiring Node.js.

## Current scope

- FastAPI application factory and CLI entrypoint
- Runtime registry CRUD backed by a local JSON file
- Runtime overview page with health, task, and A2A topology summaries
- Runtime detail page with session, task, and recent event views
- Dashboard-side proxy routes for telemetry, public agent-card fetches, narrow control actions, and runtime SSE streams
- Static asset directories wired for local BeerCSS and Vue files

## Registry format

The runtime registry lives at `PB_DASHBOARD_RUNTIME_REGISTRY_PATH` and stores dashboard-side node connection data.

Example:

```json
{
 "runtimes": [
  {
   "runtime_id": "runtime-a",
   "label": "Build runtime A",
   "base_url": "http://runtime-a:8000",
   "dashboard_bearer_token": "dashboard-token-a"
  },
  {
   "runtime_id": "runtime-b",
   "base_url": "http://runtime-b:8000"
  }
 ]
}
```

The bearer token is optional for public telemetry, but it is required if you want the dashboard to invoke runtime control endpoints.

## UI surface

- `/runtimes` for registry management, fleet health, and observed A2A edges
- `/runtimes/{runtime_id}` for per-runtime sessions, tasks, operator controls, and recent events

## Static asset drop-in points

The dashboard now expects local frontend assets here:

- `src/pillbug_dashboard/static/css/beer.min.css`
- `src/pillbug_dashboard/static/js/vendor/beer.min.js`
- `src/pillbug_dashboard/static/js/vendor/material-dynamic-colors.min.js`
- `src/pillbug_dashboard/static/js/vendor/vue.global.js`
- `src/pillbug_dashboard/static/js/app/`

The base template references the local BeerCSS and Vue vendor files directly, and page-specific scripts can be added through the template `scripts` block later.

## Run

From the repository root:

```bash
uv run pillbug-dashboard
```

Useful environment variables:

- `PB_DASHBOARD_HOST`
- `PB_DASHBOARD_PORT`
- `PB_DASHBOARD_BASE_DIR`
- `PB_DASHBOARD_RUNTIME_REGISTRY_PATH`
- `PB_DASHBOARD_RUNTIME_TIMEOUT_SECONDS`
