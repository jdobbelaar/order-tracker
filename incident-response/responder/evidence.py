"""Collect logs, traces, and metrics for an alert through Grafana's data source proxy."""
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx


def _write(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _proxy(settings, datasource_uid, path):
    return f"{settings.grafana_url}/api/datasources/proxy/uid/{datasource_uid}/{path}"


def _traceql(settings, endpoint):
    clauses = [f'resource.service.name = "{settings.service_name}"']
    if endpoint:
        escaped = endpoint.replace("\\", "\\\\").replace('"', '\\"')
        clauses.append(f'span.http.route = "{escaped}"')
    clauses.append("(span.http.status_code >= 500 || status = error)")
    return "{ " + " && ".join(clauses) + " }"


async def _logs(client, settings, start, end):
    response = await client.get(
        _proxy(settings, "loki", "loki/api/v1/query_range"),
        params={
            "query": f'{{service_name="{settings.service_name}"}}',
            "start": int(start.timestamp() * 1e9),
            "end": int(end.timestamp() * 1e9),
            "limit": 500,
            "direction": "forward",
        },
    )
    response.raise_for_status()
    entries = []
    for stream in response.json()["data"]["result"]:
        for timestamp_ns, line in stream["values"]:
            entries.append({
                "time": datetime.fromtimestamp(int(timestamp_ns) / 1e9, timezone.utc).isoformat(),
                "line": line,
                "labels": stream["stream"],
            })
    entries.sort(key=lambda entry: entry["time"])
    return entries


async def _traces(client, settings, endpoint, start, end, out_dir):
    query = _traceql(settings, endpoint)
    response = await client.get(
        _proxy(settings, "tempo", "api/search"),
        params={"q": query, "start": int(start.timestamp()), "end": int(end.timestamp()), "limit": 10},
    )
    response.raise_for_status()
    found = response.json().get("traces", [])
    saved = []
    for summary in found:
        trace_id = summary["traceID"]
        trace = await client.get(_proxy(settings, "tempo", f"api/traces/{trace_id}"))
        trace.raise_for_status()
        _write(out_dir / "traces" / f"{trace_id}.json", trace.json())
        saved.append(trace_id)
    return {"traceql": query, "matches": found, "saved_trace_ids": saved}


async def _metrics(client, settings):
    response = await client.get(
        _proxy(settings, "prometheus", "api/v1/query"),
        params={
            "query": "sum by (http_route, http_response_status_code) "
                     "(increase(order_tracker_http_requests_total[15m]))"
        },
    )
    response.raise_for_status()
    return response.json()["data"]["result"]


async def collect(settings, alert, start, end, out_dir, transport=None):
    """Save logs.json, traces.json, traces/<id>.json, and metrics.json. Return a summary.

    Each source is fetched on its own, so one that is down is recorded as an error and does not
    stop the others.
    """
    endpoint = alert.get("labels", {}).get("http_route") or alert.get("annotations", {}).get("endpoint")
    summary = {"endpoint": endpoint, "start": start.isoformat(), "end": end.isoformat(), "errors": {}}
    async with httpx.AsyncClient(transport=transport, timeout=20) as client:
        for name, fetch in (
            ("logs", lambda: _logs(client, settings, start, end)),
            ("traces", lambda: _traces(client, settings, endpoint, start, end, out_dir)),
            ("metrics", lambda: _metrics(client, settings)),
        ):
            try:
                data = await fetch()
            except Exception as error:  # noqa: BLE001 - recorded, not fatal
                summary["errors"][name] = f"{type(error).__name__}: {error}"
                continue
            _write(out_dir / f"{name}.json", data)
            summary[name] = len(data) if isinstance(data, list) else len(data["saved_trace_ids"])
    return summary
