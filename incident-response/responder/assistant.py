import asyncio
import shlex
import shutil


def build_prompt(incident, incident_dir, repo_dir):
    def rel(path):
        return path.relative_to(repo_dir).as_posix() if path.is_relative_to(repo_dir) else str(path)

    alert = incident["alert"]
    annotations = alert.get("annotations", {})
    return f"""An alert fired for the order-tracker service. Investigate it and report the root cause.

Alert: {alert.get("labels", {}).get("alertname")}
Affected endpoint: {incident["endpoint"] or "unknown"}
Evidence window: {incident["evidence"]["start"]} to {incident["evidence"]["end"]}
Summary: {annotations.get("description") or annotations.get("summary")}
Dashboard: {alert.get("dashboardURL") or annotations.get("dashboard_url")}

Evidence was saved in {rel(incident_dir)}/:
- alert.json: the alert as Grafana sent it
- logs.json: application logs from Loki for the window, oldest first
- traces.json: the Tempo search (TraceQL query and matches) for 5xx or errored requests to the endpoint
- traces/<trace id>.json: each matching trace in full (OTLP JSON; look at exception events and stack traces)
- metrics.json: request counts by route and status code over the last 15 minutes
- incident.json: what was collected, including any source that could not be fetched

The application source is in app/. Read the evidence, then read the code the stack traces point to.
You only have read tools, so do not try to change files. Answer in Markdown with these sections:
1. Summary: what is failing and who is affected.
2. Evidence: the specific log lines, spans, or numbers that show it.
3. Root cause: the code path and why it fails, with file and line references.
4. Suggested fix: what to change, and a test that would catch it.
5. Confidence: how sure you are and what you could not verify.
"""


async def run_assistant(command, prompt, cwd, stdout_path, stderr_path):
    """Run the assistant headless, feeding the prompt on stdin. Return its exit code."""
    argv = shlex.split(command, posix=True)
    executable = shutil.which(argv[0])
    if executable is None:
        raise FileNotFoundError(f"Assistant command not found on PATH: {argv[0]}")
    process = await asyncio.create_subprocess_exec(
        executable, *argv[1:],
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(prompt.encode("utf-8"))
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    return process.returncode
