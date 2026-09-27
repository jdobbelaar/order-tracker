import asyncio
import json
import shlex
import shutil


def build_prompt(incident, incident_dir, worktree):
    alert = incident["alert"]
    annotations = alert.get("annotations", {})
    return f"""An alert fired for the order-tracker service. Find the root cause, fix it, and verify the fix.

Alert: {alert.get("labels", {}).get("alertname")}
Affected endpoint: {incident["endpoint"] or "unknown"}
Evidence window: {incident["evidence"]["start"]} to {incident["evidence"]["end"]}
Summary: {annotations.get("description") or annotations.get("summary")}
Dashboard: {alert.get("dashboardURL") or annotations.get("dashboard_url")}

Evidence is saved in {incident_dir}:
- alert.json: the alert as Grafana sent it
- logs.json: application logs from Loki for the window, oldest first
- traces.json: the Tempo search (TraceQL query and matches) for 5xx or errored requests to the endpoint
- traces/<trace id>.json: each matching trace in full (OTLP JSON; look at exception events and stack traces)
- metrics.json: request counts by route and status code over the last 15 minutes
- incident.json: what was collected, including any source that could not be fetched

Your working directory is a git worktree of the repository on branch {worktree["branch"]}, created from
commit {worktree["base_commit"][:10]}. Work only inside it. The application source is in app/ and the tests are in tests/.

Do this:
1. Read the evidence, then the code the stack traces point to, and establish the root cause.
2. Make the smallest change that fixes it. Do not refactor or fix unrelated things.
3. Add a regression test in tests/ that fails without your fix and passes with it.
4. Run `uv run --frozen pytest -q` from the worktree root. Every test must pass.
5. Commit with `git add <the files you changed>` and then `git commit -m "<one-line message>"`. Use a single-line message.
   Do not push, do not deploy, and do not touch anything outside the worktree.

If you cannot find the cause or cannot make the tests pass, do not commit. Say so and explain what you found.

Finish with a Markdown report with these sections:
1. Summary: what was failing and who was affected.
2. Root cause: the code path and why it failed, with file and line references.
3. Fix: what you changed and the test you added.
4. Verification: the test command you ran and its result.
5. Confidence: how sure you are, and what a person still has to do (review, merge, deploy).
"""


def parse_output(text):
    """Split `claude -p --output-format json` output into the report and run details.

    Any other output (for example from a different assistant command) is kept as the report.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return text, {}
    if not isinstance(data, dict) or "type" not in data:
        return text, {}
    usage = {
        key: data[key]
        for key in ("subtype", "is_error", "total_cost_usd", "num_turns", "duration_ms", "usage")
        if key in data
    }
    return data.get("result") or text, usage


async def run_assistant(command, prompt, cwd, stderr_path, incident_dir, max_budget_usd):
    """Run the assistant headless, feeding the prompt on stdin. Return its exit code and stdout."""
    command = command.replace("{incident_dir}", incident_dir.as_posix())
    command = command.replace("{max_budget_usd}", str(max_budget_usd))
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
    stderr_path.write_bytes(stderr)
    return process.returncode, stdout.decode("utf-8", errors="replace")
