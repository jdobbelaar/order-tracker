"""Stands in for the coding assistant. It runs in the incident's worktree, like the real one.

Reads the prompt on stdin, commits a file unless given --no-commit, and prints a result in the
same JSON shape as `claude -p --output-format json`.
"""
import json
import subprocess
import sys

prompt = sys.stdin.read()
if "--no-commit" not in sys.argv:
    with open("fix.txt", "w", encoding="utf-8") as handle:
        handle.write("fixed\n")
    subprocess.run(["git", "add", "fix.txt"], check=True)
    subprocess.run(["git", "commit", "-q", "-m", "Fix the reported problem"], check=True)

print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "FAKE REPORT\n" + prompt.splitlines()[0],
    "total_cost_usd": 0.12,
    "num_turns": 3,
}))
