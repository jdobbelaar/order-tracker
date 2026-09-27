import asyncio
import hmac
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request

from responder.assistant import build_prompt, parse_output, run_assistant
from responder.config import Settings
from responder.evidence import collect
from responder.fix import create_worktree, describe_fix

logger = logging.getLogger("responder")


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "unknown").lower()).strip("-")[:40] or "unknown"


def _update(incident_dir, **changes):
    path = incident_dir / "incident.json"
    incident = json.loads(path.read_text(encoding="utf-8"))
    incident.update(changes)
    path.write_text(json.dumps(incident, indent=2), encoding="utf-8")
    return incident


def _alert_start(alert, fallback):
    try:
        start = datetime.fromisoformat(alert["startsAt"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, ValueError):
        return fallback
    return start if start.year >= 2000 else fallback  # Grafana uses 0001-01-01 for "unset"


async def investigate(settings, incident_dir, transport):
    incident = json.loads((incident_dir / "incident.json").read_text(encoding="utf-8"))
    try:
        start = datetime.fromisoformat(incident["window_start"])
        end = datetime.now(timezone.utc) + timedelta(minutes=1)
        summary = await collect(settings, incident["alert"], start, end, incident_dir, transport)
        worktree = await create_worktree(settings, incident_dir, incident["id"])
        incident = _update(incident_dir, status="evidence_collected", evidence=summary, worktree=worktree)
        prompt = build_prompt(incident, incident_dir.resolve(), worktree)
        (incident_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        _update(incident_dir, status="assistant_running")
        code, stdout = await run_assistant(
            settings.assistant_command, prompt, worktree["path"],
            incident_dir / "assistant.stderr.log", incident_dir.resolve(), settings.max_budget_usd,
        )
        report, usage = parse_output(stdout)
        (incident_dir / "report.md").write_text(report, encoding="utf-8")
        fix, diff = await describe_fix(worktree)
        if diff:
            (incident_dir / "fix.diff").write_text(diff + "\n", encoding="utf-8")
        if code != 0 or usage.get("is_error"):
            status = "assistant_failed"
        else:
            status = "fix_committed" if fix["commits"] else "no_fix"
        _update(incident_dir, status=status, assistant_exit_code=code, assistant=usage, fix=fix)
    except Exception as error:  # noqa: BLE001 - keep the failure with the incident
        logger.exception("Investigation failed for %s", incident_dir.name)
        _update(incident_dir, status="failed", error=f"{type(error).__name__}: {error}")


def create_app(settings=None, transport=None):
    settings = settings or Settings.from_env()
    app = FastAPI(title="Incident Response")
    tasks = set()
    handled = {}  # fingerprint -> {"id", "dir", "at"}

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.post("/alerts", status_code=202)
    async def receive_alerts(request: Request):
        # This endpoint starts an assistant that can edit code, so only Grafana (which holds the
        # token) may call it. An unset token rejects everything.
        expected = f"Bearer {settings.webhook_token}"
        given = request.headers.get("authorization", "")
        if not settings.webhook_token or not hmac.compare_digest(given.encode(), expected.encode()):
            raise HTTPException(401, "Missing or invalid bearer token")
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, "Body must be JSON") from None
        alerts = payload.get("alerts") if isinstance(payload, dict) else None
        if not isinstance(alerts, list):
            raise HTTPException(422, "Expected a Grafana webhook payload with an alerts list")

        started, skipped = [], []
        now = time.monotonic()
        for alert in alerts:
            labels = alert.get("labels", {})
            endpoint = labels.get("http_route") or alert.get("annotations", {}).get("endpoint")
            fingerprint = alert.get("fingerprint") or _slug(f"{labels.get('alertname')}-{endpoint}")
            previous = handled.get(fingerprint)

            if alert.get("status") == "resolved":
                if previous:
                    _update(previous["dir"], resolved_at=alert.get("endsAt"))
                skipped.append({"fingerprint": fingerprint, "reason": "resolved"})
                continue
            if previous and now - previous["at"] < settings.cooldown_seconds:
                skipped.append({"fingerprint": fingerprint, "reason": "already handled",
                                "incident": previous["id"]})
                continue

            received = datetime.now(timezone.utc)
            incident_id = f"{received:%Y%m%dT%H%M%SZ}-{_slug(endpoint)}-{fingerprint[:8]}"
            incident_dir = settings.incidents_dir / incident_id
            incident_dir.mkdir(parents=True, exist_ok=True)
            (incident_dir / "alert.json").write_text(json.dumps(alert, indent=2), encoding="utf-8")
            window_start = _alert_start(alert, received) - timedelta(minutes=settings.lookback_minutes)
            (incident_dir / "incident.json").write_text(json.dumps({
                "id": incident_id,
                "status": "received",
                "received_at": received.isoformat(),
                "endpoint": endpoint,
                "fingerprint": fingerprint,
                "window_start": window_start.isoformat(),
                "alert": alert,
            }, indent=2), encoding="utf-8")

            handled[fingerprint] = {"id": incident_id, "dir": incident_dir, "at": now}
            task = asyncio.create_task(investigate(settings, incident_dir, transport))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            started.append({"id": incident_id, "endpoint": endpoint})

        return {"started": started, "skipped": skipped}

    return app


app = create_app()
