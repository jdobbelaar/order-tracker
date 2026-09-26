import os
from dataclasses import dataclass
from pathlib import Path


# Read-only tools: the assistant diagnoses the incident and does not change anything.
DEFAULT_ASSISTANT_COMMAND = (
    "claude -p --permission-mode dontAsk --allowedTools Read Grep Glob "
    "--no-session-persistence --max-budget-usd 2"
)


@dataclass(frozen=True)
class Settings:
    grafana_url: str
    incidents_dir: Path
    repo_dir: Path
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
            assistant_command=os.getenv("INCIDENT_ASSISTANT_COMMAND", DEFAULT_ASSISTANT_COMMAND),
            lookback_minutes=int(os.getenv("INCIDENT_LOOKBACK_MINUTES", "10")),
            cooldown_seconds=int(os.getenv("INCIDENT_COOLDOWN_SECONDS", "3600")),
            service_name=os.getenv("INCIDENT_SERVICE_NAME", "order-tracker"),
        )
