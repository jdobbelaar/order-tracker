# Incident response

A small service that receives Grafana alerts, saves the evidence needed to understand the problem, and starts the coding assistant (Claude Code) in headless mode to investigate.

## How it works

1. Grafana's **incident-response** contact point posts a webhook to `POST /alerts` on port 8001. The contact point and notification policy are provisioned in `observability/grafana/provisioning/alerting/contact-points.yaml`.
2. For each firing alert, the service creates `incidents/<time>-<endpoint>-<fingerprint>/` and answers `202` straight away. Everything after that runs in the background.
3. It collects evidence through Grafana's data source proxy (so only Grafana's port is needed), covering the alert's start minus 10 minutes up to now:
   - `alert.json`: the alert as Grafana sent it, including the affected endpoint (`http_route` label).
   - `logs.json`: the app's logs from Loki, oldest first. This includes tracebacks from unhandled exceptions, each with its `trace_id`.
   - `traces.json` and `traces/<trace id>.json`: Tempo traces of 5xx or errored requests to that endpoint, in full.
   - `metrics.json`: request counts by route and status code over the last 15 minutes.
   - If one source is down, the error is recorded in `incident.json` and the rest are still saved.
4. It writes `prompt.md`, then runs the assistant with the prompt on stdin, from the repository root so it can read `app/`. The assistant's answer is saved as `report.md`, and its stderr as `assistant.stderr.log`.
5. `incident.json` tracks the state: `received`, `evidence_collected`, `assistant_running`, then `assistant_done`, `assistant_failed`, or `failed`.

Resolved notifications do not start the assistant. They add `resolved_at` to the matching incident. A fingerprint that was already handled is skipped for an hour, because Grafana repeats firing notifications while an alert stays open.

The assistant runs with read-only tools (`Read`, `Grep`, `Glob`), so it diagnoses and suggests a fix but changes nothing.

## Run it

The service runs on the host, not in Compose, because it needs the `claude` CLI and its login. Compose still has to be running, since Grafana is where the evidence comes from.

```bash
docker compose up --build -d --wait
cd incident-response
uv run --frozen uvicorn responder.main:app --host 127.0.0.1 --port 8001
```

Grafana reaches the host through `host.docker.internal` (Compose maps it with `extra_hosts`). Binding to `127.0.0.1` worked with Docker Desktop on Windows. On Linux, bind to `0.0.0.0` or the Docker bridge address instead, and firewall the port, because the endpoint has no authentication.

To try it, trigger a 5xx and wait for the alert to fire (about 2 minutes):

```bash
curl http://127.0.0.1:8000/api/orders/express-1002
```

You can also post a payload by hand: `curl -X POST localhost:8001/alerts -H 'Content-Type: application/json' -d @alert.json`, where the file has an `alerts` list in Grafana's webhook format.

## Settings

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `GRAFANA_URL` | `http://127.0.0.1:3000` | Where to fetch logs, traces, and metrics |
| `INCIDENT_DIR` | `incident-response/incidents` | Where incidents are saved (git-ignored) |
| `INCIDENT_REPO_DIR` | the repository root | Working directory for the assistant |
| `INCIDENT_ASSISTANT_COMMAND` | `claude -p --permission-mode dontAsk --allowedTools Read Grep Glob --no-session-persistence --max-budget-usd 2` | The headless command. The prompt arrives on stdin. It is split like a shell command line, so use forward slashes in Windows paths. |
| `INCIDENT_LOOKBACK_MINUTES` | `10` | Evidence window before the alert started |
| `INCIDENT_COOLDOWN_SECONDS` | `3600` | Ignore repeat notifications for the same alert |
| `INCIDENT_SERVICE_NAME` | `order-tracker` | Service name used in the Loki and Tempo queries |

## Tests

```bash
cd incident-response
uv run --frozen pytest -q
```

The tests use a fake Grafana and a fake assistant, so they need neither Compose nor the `claude` CLI.
