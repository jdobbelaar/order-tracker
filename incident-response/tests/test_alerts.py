import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from responder.assistant import parse_output
from responder.config import DEFAULT_ASSISTANT_COMMAND, Settings
from responder.main import create_app

FAKE_ASSISTANT = Path(__file__).parent / "fake_assistant.py"
FAKE_COMMAND = f"{Path(sys.executable).as_posix()} {FAKE_ASSISTANT.as_posix()}"
TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
TRACE_ID = "a" * 32
DONE = {"fix_committed", "no_fix", "assistant_failed", "failed"}


def fake_grafana(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("loki/api/v1/query_range"):
        return httpx.Response(200, json={"data": {"result": [{
            "stream": {"service_name": "order-tracker", "trace_id": TRACE_ID},
            "values": [["1790463382516234964", "Order lookup succeeded"]],
        }]}})
    if path.endswith("/api/search"):
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


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    (path / "app.txt").write_text("original\n")
    git(path, "add", "app.txt")
    git(path, "commit", "-q", "-m", "Initial commit")
    return path


@pytest.fixture
def make_client(tmp_path, repo):
    def make(command=FAKE_COMMAND, transport=httpx.MockTransport(fake_grafana), token=TOKEN, headers=AUTH):
        settings = Settings(
            grafana_url="http://grafana.test",
            incidents_dir=tmp_path / "incidents",
            repo_dir=repo,
            base_ref="HEAD",
            webhook_token=token,
            max_budget_usd=0.5,
            assistant_command=command,
            lookback_minutes=10,
            cooldown_seconds=3600,
            service_name="order-tracker",
        )
        return TestClient(create_app(settings, transport), headers=headers), tmp_path / "incidents"
    return make


def wait_for_status(incidents_dir, wanted=DONE, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for incident_file in incidents_dir.glob("*/incident.json"):
            incident = json.loads(incident_file.read_text(encoding="utf-8"))
            if incident["status"] in wanted:
                return incident_file.parent, incident
        time.sleep(0.1)
    raise AssertionError(f"No incident reached {wanted}")


def test_firing_alert_saves_evidence_and_commits_a_fix_on_its_own_branch(make_client, repo):
    test_client, incidents = make_client()
    with test_client as client:
        response = client.post("/alerts", json={"status": "firing", "alerts": [alert()]})
        assert response.status_code == 202
        assert response.json()["started"][0]["endpoint"] == "/api/orders/{order_id}"
        incident_dir, incident = wait_for_status(incidents)

    assert incident["status"] == "fix_committed", incident
    assert incident["endpoint"] == "/api/orders/{order_id}"
    assert incident["evidence"]["errors"] == {}
    assert json.loads((incident_dir / "alert.json").read_text())["fingerprint"] == "abc123def456"
    logs = json.loads((incident_dir / "logs.json").read_text())
    assert logs[0]["line"] == "Order lookup succeeded"
    assert logs[0]["labels"]["trace_id"] == TRACE_ID
    assert (incident_dir / "traces" / f"{TRACE_ID}.json").exists()
    assert json.loads((incident_dir / "traces.json").read_text())["saved_trace_ids"] == [TRACE_ID]
    assert json.loads((incident_dir / "metrics.json").read_text())[0]["value"][1] == "3"
    assert "/api/orders/{order_id}" in (incident_dir / "prompt.md").read_text()

    # The assistant's JSON output becomes the report, and its cost is recorded.
    assert (incident_dir / "report.md").read_text().startswith("FAKE REPORT")
    assert incident["assistant"]["total_cost_usd"] == 0.12

    # The fix is committed on the incident branch, and the checked-out branch is untouched.
    branch = incident["worktree"]["branch"]
    assert branch.startswith("incident/")
    assert incident["fix"]["commits"][0]["subject"] == "Fix the reported problem"
    assert "fix.txt" in (incident_dir / "fix.diff").read_text()
    assert git(repo, "log", branch, "--format=%s").splitlines()[0] == "Fix the reported problem"
    assert not (repo / "fix.txt").exists()
    assert git(repo, "log", "--format=%s").splitlines() == ["Initial commit"]


def test_assistant_that_makes_no_commit_is_reported_as_no_fix(make_client):
    test_client, incidents = make_client(command=f"{FAKE_COMMAND} --no-commit")
    with test_client as client:
        client.post("/alerts", json={"alerts": [alert()]})
        incident_dir, incident = wait_for_status(incidents)
    assert incident["status"] == "no_fix"
    assert incident["fix"]["commits"] == []
    assert not (incident_dir / "fix.diff").exists()


def test_alert_without_the_token_is_rejected_and_starts_nothing(make_client):
    test_client, incidents = make_client(headers={})
    with test_client as client:
        assert client.post("/alerts", json={"alerts": [alert()]}).status_code == 401
        wrong = {"Authorization": "Bearer wrong"}
        assert client.post("/alerts", json={"alerts": [alert()]}, headers=wrong).status_code == 401
    assert not incidents.exists()


def test_service_with_no_configured_token_rejects_everything(make_client):
    test_client, incidents = make_client(token="", headers={"Authorization": "Bearer "})
    with test_client as client:
        assert client.post("/alerts", json={"alerts": [alert()]}).status_code == 401
        assert client.get("/healthz").status_code == 200
    assert not incidents.exists()


def test_duplicate_firing_alert_is_skipped(make_client):
    test_client, incidents = make_client()
    with test_client as client:
        first = client.post("/alerts", json={"alerts": [alert()]}).json()
        second = client.post("/alerts", json={"alerts": [alert()]}).json()
        wait_for_status(incidents)
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
        incident_dir, _ = wait_for_status(incidents)
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
        _, incident = wait_for_status(incidents)
    assert incident["status"] == "fix_committed"
    assert set(incident["evidence"]["errors"]) == {"logs", "traces", "metrics"}


def test_missing_assistant_command_marks_incident_failed(make_client):
    test_client, incidents = make_client(command="no-such-assistant-binary -p")
    with test_client as client:
        client.post("/alerts", json={"alerts": [alert()]})
        _, incident = wait_for_status(incidents)
    assert incident["status"] == "failed"
    assert "not found" in incident["error"]


def test_rejects_bodies_that_are_not_grafana_payloads(make_client):
    test_client, _ = make_client()
    with test_client as client:
        assert client.post("/alerts", content=b"not json").status_code == 400
        assert client.post("/alerts", json={"nope": 1}).status_code == 422


def test_parse_output_reads_claude_json_and_keeps_anything_else():
    tokens = {"input_tokens": 120, "output_tokens": 340, "cache_read_input_tokens": 9000}
    raw = json.dumps({"type": "result", "result": "the report", "total_cost_usd": 0.4, "num_turns": 7,
                      "usage": tokens})
    assert parse_output(raw) == ("the report", {"total_cost_usd": 0.4, "num_turns": 7, "usage": tokens})
    assert parse_output("plain text report") == ("plain text report", {})


def test_default_command_can_edit_and_commit_but_not_push():
    assert "Edit" in DEFAULT_ASSISTANT_COMMAND and "Bash(git commit:*)" in DEFAULT_ASSISTANT_COMMAND
    assert "Bash(git push:*)" in DEFAULT_ASSISTANT_COMMAND.split("--disallowedTools")[1]
    assert "--allowedTools" in DEFAULT_ASSISTANT_COMMAND.split("--disallowedTools")[0]
