# AAP Bridge UI (optional)

Small React + Vite + PatternFly web console for AAP Bridge. It talks only to the
existing FastAPI server (`GET /api/v1/openapi.json`) and is served by an optional
second container (`aap-bridge-ui`) that reverse-proxies `/api/v1` to the API
container — no API CORS change required.

Relation to [ansible/ansible-ui](https://github.com/ansible/ansible-ui): that repo
is a full AWX/EDA/Hub monorepo whose Job Output view is hard-wired to AWX's
`/api/v2/jobs` + `job_events` backend, so it cannot be embedded as a component.
This UI instead reuses the same design system (PatternFly, including
`@patternfly/react-log-viewer`, the component family AWX uses for stdout) and
mimics the AWX Job Output UX — dark log surface, line numbers, event filter,
search, autoscroll, live polling, download — against the migration-job API
(`GET /jobs/{id}/console`, `GET /jobs/{id}/artifacts`).

## Screens (full migration workflow)

- **Dashboard** — API health, migration state summary, recent jobs (warns when no pair is configured).
- **Settings** — store any number of source/target endpoints (encrypted, tested live), pick the active migration pair. First load auto-routes here until a pair is configured.
- **Migrate** — full migration, export-only, granular import (chained `job_id`), with per-submit source/target pickers.
- **Jobs** — live list with status filter (AWX-style status dots).
- **Job detail** — status/result, AWX-style output viewer, chain next phase
  (transform/import/validate/report), artifact downloads.
- **Validate & reports** — validation runs, migration/enhanced HTML reports.

## Local dev (no containers)

```bash
# 1. Start the API (from repo root)
export AAP_BRIDGE_API_TOKEN=dev-token-change-me
export AAP_BRIDGE_ALLOW_ANON=1  # optional: skip auth locally
aap-bridge-api  # serves http://127.0.0.1:8000, docs at /api/v1/docs

# 2. Start the UI
cd ui
npm ci
npm run dev  # http://localhost:3000, /api proxied to :8000 (see vite.config.ts)
```

Set the API key in the UI header (value of `AAP_BRIDGE_API_TOKEN`); it is stored
only in `localStorage` and sent as `X-API-Key`.

Useful env vars:

| Var | Purpose | Default |
| --- | --- | --- |
| `VITE_API_BASE` | API prefix the browser calls (same-origin in containers) | `/api/v1` |
| `AAP_BRIDGE_API_URL` | Dev-proxy target for `/api` (Vite only) | `http://127.0.0.1:8000` |

## Optional container

```bash
cd container
cp .env.container .env  # set ONLY AAP_BRIDGE_API_TOKEN; source/target go in Setup
podman-compose --profile ui up -d --build
# UI:  http://localhost:8080  → Setup page for source/target endpoints
# API: http://localhost:8000/api/v1/docs
```

`container/docker-compose.yml` keeps the default (CLI-only `aap-bridge` service)
unchanged; the `ui` profile adds:

- `aap-bridge-api` — same image as the CLI container, running `aap-bridge-api`
  on `:8000` with the mounted `.env`.
- `aap-bridge-ui` — this app on `:8080`, proxying `/api/` to `aap-bridge-api`.

## Build the UI image alone

```bash
podman build -f ui/Containerfile -t aap-bridge-ui:latest .
```

## Notes

- Job submits return `202 {job_id, poll_url}`; the UI polls `GET /jobs/{id}`
  every 3s and `GET /jobs/{id}/console?tail=5000` every 2s while queued/running.
- Console text is plain `console.log` capture (click echo), not AWX `job_events`,
  so there is no per-host event stream — the event filter is client-side text
  matching over the same dark LogViewer surface.
- License note: PatternFly is MIT; no ansible-ui code is vendored, so there is
  no Apache-2.0/GPL-3.0 mixing concern. If you later copy ansible-ui components,
  keep their Apache-2.0 headers and record it in `LICENSE`/`NOTICE`.
