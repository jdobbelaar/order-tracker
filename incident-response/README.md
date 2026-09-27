# Incident response

A small service that receives Grafana alerts, saves the evidence needed to understand the problem, and starts the coding assistant (Claude Code) in headless mode to find the cause and fix it on a branch.

## How it works

1. Grafana's **incident-response** contact point posts a webhook to `POST /alerts` on port 8001, with a bearer token. The contact point and notification policy are provisioned in `observability/grafana/provisioning/alerting/contact-points.yaml`.
2. Requests without the right token get `401`, and the service rejects everything if no token is configured. The endpoint starts an assistant that can edit code, so only Grafana should be able to call it.
3. For each firing alert, the service creates `incidents/<time>-<endpoint>-<fingerprint>/` and answers `202` straight away. Everything after that runs in the background.
4. It collects evidence through Grafana's data source proxy (so only Grafana's port is needed), covering the alert's start minus 10 minutes up to now:
   - `alert.json`: the alert as Grafana sent it, including the affected endpoint (`http_route` label).
   - `logs.json`: the app's logs from Loki, oldest first. This includes tracebacks from unhandled exceptions, each with its `trace_id`.
   - `traces.json` and `traces/<trace id>.json`: Tempo traces of 5xx or errored requests to that endpoint, in full.
   - `metrics.json`: request counts by route and status code over the last 15 minutes.
   - If one source is down, the error is recorded in `incident.json` and the rest are still saved.
5. It creates a git worktree at `incidents/<id>/worktree` on a new branch `incident/<id>`, based on the repository's current `HEAD`. Your own checkout and branch are never touched.
6. It writes `prompt.md`, then runs the assistant in the worktree with the prompt on stdin. The assistant reads the evidence, finds the root cause, makes the smallest fix, adds a regression test, runs `uv run --frozen pytest -q`, and commits on the incident branch.
7. `report.md` holds the assistant's report, `fix.diff` holds what it committed, and `incident.json` records the outcome and the run's cost, turns, duration, and token counts (`assistant.usage`).

`incident.json` status values:

| Status | Meaning |
| --- | --- |
| `received`, `evidence_collected`, `assistant_running` | In progress |
| `fix_committed` | The assistant committed a fix on the incident branch. Review it, then merge and deploy. |
| `no_fix` | The assistant finished without committing, usually because it could not find or verify a fix. Read `report.md`. |
| `assistant_failed` | The assistant exited with an error or hit its spending cap. |
| `failed` | The service itself failed, for example the worktree could not be created. See `error`. |

A fix is only known to work once the alert clears after you merge and deploy it. Grafana then sends a resolved notification, which the service records as `resolved_at` on the incident. Resolved notifications never start the assistant, and a fingerprint that was already handled is skipped for an hour, because Grafana repeats firing notifications while an alert stays open.

## What the assistant can and cannot do

The default command allows `Read`, `Grep`, `Glob`, `Edit`, and `Write`, plus only these shell commands: `uv run --frozen pytest`, `git status`, `git diff`, `git add`, and `git commit`. `git push` is explicitly denied. It runs with `--permission-mode dontAsk`, so anything not listed is refused, and it may use only the worktree and the incident folder. A run is capped at `INCIDENT_MAX_BUDGET_USD` (default $2). A real run reported about $0.54 (14 turns, under 3 minutes), so a cap of $0.25 would cut runs off before they finish. That dollar figure is an estimate at API list prices. When `claude` is logged in with a subscription and no `ANTHROPIC_API_KEY` is set, runs count against your plan's usage limits instead of being billed.

This is not a sandbox. The assistant runs tests it writes, and test code can do anything your user account can. Nothing is pushed or deployed, and you review every fix.

## Run it

The service runs on the host, not in Compose, because it needs the `claude` CLI and its login. Compose still has to be running, since Grafana is where the evidence comes from.

1. Create a token in the repository's `.env` file (git-ignored). Compose gives it to Grafana, and the service reads it from its environment:

   ```bash
   echo "INCIDENT_WEBHOOK_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" > .env
   ```

2. Start the stack and the service:

   ```bash
   docker compose up --build -d --wait
   cd incident-response
   set -a && . ../.env && set +a
   uv run --frozen uvicorn responder.main:app --host 127.0.0.1 --port 8001
   ```

   In PowerShell, set the token instead of sourcing the file: `$env:INCIDENT_WEBHOOK_TOKEN = "<the value from .env>"`.

Grafana reaches the host through `host.docker.internal` (Compose maps it with `extra_hosts`). Binding to `127.0.0.1` worked with Docker Desktop on Windows. On Linux, bind to `0.0.0.0` or the Docker bridge address instead, and firewall the port.

To try it, trigger a 5xx and wait for the alert to fire (about 2 minutes):

```bash
curl http://127.0.0.1:8000/api/orders/express-1002
```

To post an alert by hand from PowerShell, send the token too. Note that a firing alert starts a real, paid run.

```powershell
$body = @{ alerts = @(@{ status = "firing"; labels = @{ alertname = "Test"; http_route = "/api/orders/{order_id}" } }) } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri http://localhost:8001/alerts -ContentType "application/json" `
  -Headers @{ Authorization = "Bearer $env:INCIDENT_WEBHOOK_TOKEN" } -Body $body
```

When you are done with an incident, remove its worktree and branch:

```bash
git worktree remove --force incident-response/incidents/<id>/worktree
git branch -D incident/<id>
```

## Settings

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `INCIDENT_WEBHOOK_TOKEN` | none (required) | Bearer token Grafana must send. Without it every alert is rejected. |
| `GRAFANA_URL` | `http://127.0.0.1:3000` | Where to fetch logs, traces, and metrics |
| `INCIDENT_DIR` | `incident-response/incidents` | Where incidents and worktrees are saved (git-ignored) |
| `INCIDENT_REPO_DIR` | the repository root | The repository the worktree is created from |
| `INCIDENT_BASE_REF` | `HEAD` | What the incident branch starts from |
| `INCIDENT_MAX_BUDGET_USD` | `2` | Spending cap for one assistant run |
| `INCIDENT_ASSISTANT_COMMAND` | see `responder/config.py` | The headless command. The prompt arrives on stdin, and `{incident_dir}` and `{max_budget_usd}` are filled in. It is split like a shell command line, so use forward slashes in Windows paths. |
| `INCIDENT_LOOKBACK_MINUTES` | `10` | Evidence window before the alert started |
| `INCIDENT_COOLDOWN_SECONDS` | `3600` | Ignore repeat notifications for the same alert |
| `INCIDENT_SERVICE_NAME` | `order-tracker` | Service name used in the Loki and Tempo queries |

## Tests

```bash
cd incident-response
uv run --frozen pytest -q
```

The tests use a fake Grafana, a fake assistant, and a throwaway git repository, so they need neither Compose nor the `claude` CLI.
