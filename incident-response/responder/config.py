import os
from dataclasses import dataclass
from pathlib import Path


# The assistant works in a git worktree on its own branch. It can edit files, run the tests, and
# commit there. It cannot push. {incident_dir} (the evidence and the worktree) is the only
# directory it may use besides the worktree itself, and {max_budget_usd} caps the cost of a run.
DEFAULT_ASSISTANT_COMMAND = (
    "claude -p --output-format json --permission-mode dontAsk --no-session-persistence "
    "--max-budget-usd {max_budget_usd} --add-dir {incident_dir} "
    '--allowedTools Read Grep Glob Edit Write "Bash(uv run --frozen pytest:*)" '
    '"Bash(git status:*)" "Bash(git diff:*)" "Bash(git add:*)" "Bash(git commit:*)" '
    '--disallowedTools "Bash(git push:*)"'
)


@dataclass(frozen=True)
class Settings:
    grafana_url: str
    incidents_dir: Path
    repo_dir: Path
    base_ref: str
    webhook_token: str
    max_budget_usd: float
    assistant_command: str
    lookback_minutes: int
    cooldown_seconds: int
    service_name: str

    @classmethod
    def from_env(cls):
        service_dir = Path(__file__).resolve().parent.parent
        return cls(
            grafana_url=os.getenv("GRAFANA_URL", "http://127.0.0.1:3000").rstrip("/"),
            incidents_dir=Path(os.getenv("INCIDENT_DIR", service_dir / "incidents")),
            repo_dir=Path(os.getenv("INCIDENT_REPO_DIR", service_dir.parent)),
            base_ref=os.getenv("INCIDENT_BASE_REF", "HEAD"),
            webhook_token=os.getenv("INCIDENT_WEBHOOK_TOKEN", ""),
            max_budget_usd=float(os.getenv("INCIDENT_MAX_BUDGET_USD", "2")),
            assistant_command=os.getenv("INCIDENT_ASSISTANT_COMMAND", DEFAULT_ASSISTANT_COMMAND),
            lookback_minutes=int(os.getenv("INCIDENT_LOOKBACK_MINUTES", "10")),
            cooldown_seconds=int(os.getenv("INCIDENT_COOLDOWN_SECONDS", "3600")),
            service_name=os.getenv("INCIDENT_SERVICE_NAME", "order-tracker"),
        )
