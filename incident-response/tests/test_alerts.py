import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from responder.config import Settings
from responder.main import create_app

FAKE_ASSISTANT = Path(__file__).parent / "fake_assistant.py"
TRACE_ID = "a" * 32


def fake_grafana(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("loki/api/v1/query_range"):
        return httpx.Response(200, json={"data": {"result": [{
            "stream": {"service_name": "order-tracker", "trace_id": TRACE_ID},
            "values": [["1790463382516234964", "Order lookup succeeded"]],
        }]}})
    if path.endswith("tempo/api/search") or path.endswith("/api/search"):
        assert "span.http.route" in request.url.params["q"]
        return httpx.Response(200, json={"traces": [{"traceID": TRACE_ID}]})
    if path.endswith(f"/api/traces/{TRACE_ID}"):
        return httpx.Response(200, json={"batches": []})
    if path.endswith("api/v1/query"):
        return httpx.Response(200, json={"data": {"result": [{"metric": {}, "value": [0, "3"]}]}})
    return httpx.Response(404)


def alert(status="firing", fingerprint="abc123def456", route="/api/orders/{order_id}"):
    return {
        "status": status,
        "labels": {"alertname": "Order Tracker 5xx responses", "http_route": route},
        "annotations": {"description": f"{route} returned 3 5xx response(s) in the last 5 minutes."},
        "startsAt": "2026-09-26T23:10:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": fingerprint,
    }


@pytest.fixture
def make_client(tmp_path):
    def make(command=f"{Path(sys.executable).as_posix()} {FAKE_ASSISTANT.as_posix()}", transport=httpx.MockTransport(fake_grafana)):
        settings = Settings(
            grafana_url="http://grafana.test",
            incidents_dir=tmp_path / "incidents",
            repo_dir=tmp_path,
            assistant_command=command,
            lookback_minutes=10,
            cooldown_seconds=3600,
            service_name="order-tracker",
        )
        return TestClient(create_app(settings, transport)), tmp_path / "incidents"
    return make


def wait_for_status(incidents_dir, wanted, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for incident_file in incidents_dir.glob("*/incident.json"):
            incident = json.loads(incident_file.read_text(encoding="utf-8"))
            if incident["status"] in wanted:
                return incident_file.parent, incident
        time.sleep(0.1)
    raise AssertionError(f"No incident reached {wanted}")


def test_firing_alert_flow(make_client):
    test_client, incidents = make_client()
    with test_client as client:
        response = client.post("/alerts", json={"status": "firing", "alerts": [alert()]})
        assert response.status_code == 202
        started = response.json()["started"]
        assert started[0]["endpoint"] == "/api/orders/{order_id}"

        incident_dir, incident = wait_for_status(incidents, {"assistant_done", "assistant_failed", "failed"})

    assert incident["status"] == "assistant_done", incident
    assert incident["endpoint"] == "/api/orders/{order_id}"
    assert incident["evidence"]["errors"] == {}
    assert json.loads((incident_dir / "alert.json").read_text())["fingerprint"] == "abc123def456"
    logs = json.loads((incident_dir / "logs.json").read_text())
    assert logs[0]["line"] == "Order lookup succeeded"
    assert logs[0]["labels"]["trace_id"] == TRACE_ID
    assert (incident_dir / "traces" / f"{TRACE_ID}.json").exists()
    assert json.loads((incident_dir / "traces.json").read_text())["saved_trace_ids"] == [TRACE_ID]
    assert json.loads((incident_dir / "metrics.json").read_text())[0]["value"][1] == "3"
    prompt = (incident_dir / "prompt.md").read_text()
    assert "/api/orders/{order_id}" in prompt
    assert "FAKE REPORT" in (incident_dir / "report.md").read_text()


def test_duplicate_firing_alert_is_skipped(make_client):
    test_client, incidents = make_client()
    with test_client as client:
        first = client.post("/alerts", json={"alerts": [alert()]}).json()
        second = client.post("/alerts", json={"alerts": [alert()]}).json()
        wait_for_status(incidents, {"assistant_done", "failed"})
    assert len(first["started"]) == 1
    assert second["started"] == []
    assert second["skipped"][0]["reason"] == "already handled"
    assert len(list(incidents.glob("*/incident.json"))) == 1


def test_resolved_alert_does_not_start_assistant(make_client):
    test_client, incidents = make_client()
    with test_client as client:
        response = client.post("/alerts", json={"alerts": [alert(status="resolved")]})
    assert response.status_code == 202
    assert response.json()["started"] == []
    assert not incidents.exists() or not list(incidents.glob("*"))


def test_resolved_alert_is_recorded_on_the_incident(make_client):
    test_client, incidents = make_client()
    with test_client as client:
        client.post("/alerts", json={"alerts": [alert()]})
        incident_dir, _ = wait_for_status(incidents, {"assistant_done", "failed"})
        resolved = alert(status="resolved")
        resolved["endsAt"] = "2026-09-26T23:20:00Z"
        client.post("/alerts", json={"alerts": [resolved]})
    incident = json.loads((incident_dir / "incident.json").read_text())
    assert incident["resolved_at"] == "2026-09-26T23:20:00Z"


def test_unreachable_grafana_is_recorded_and_assistant_still_runs(make_client):
    def down(request):
        raise httpx.ConnectError("grafana is down")

    test_client, incidents = make_client(transport=httpx.MockTransport(down))
    with test_client as client:
        client.post("/alerts", json={"alerts": [alert()]})
        _, incident = wait_for_status(incidents, {"assistant_done", "assistant_failed", "failed"})
    assert incident["status"] == "assistant_done"
    assert set(incident["evidence"]["errors"]) == {"logs", "traces", "metrics"}


def test_missing_assistant_command_marks_incident_failed(make_client):
    test_client, incidents = make_client(command="no-such-assistant-binary -p")
    with test_client as client:
        client.post("/alerts", json={"alerts": [alert()]})
        _, incident = wait_for_status(incidents, {"assistant_done", "assistant_failed", "failed"})
    assert incident["status"] == "failed"
    assert "not found" in incident["error"]


def test_rejects_bodies_that_are_not_grafana_payloads(make_client):
    test_client, _ = make_client()
    with test_client as client:
        assert client.post("/alerts", content=b"not json").status_code == 400
        assert client.post("/alerts", json={"nope": 1}).status_code == 422
