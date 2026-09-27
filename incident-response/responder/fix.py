"""Give the assistant its own git worktree and branch, and report what it committed there."""
import asyncio
import subprocess


async def _git(cwd, *args):
    def run():
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False
        )
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    return await asyncio.to_thread(run)


# Adding worktrees concurrently makes git fight over its lock files.
_worktree_lock = asyncio.Lock()


async def create_worktree(settings, incident_dir, incident_id):
    """Create incident/<id> from the repository's base ref, checked out under the incident."""
    path = incident_dir / "worktree"
    branch = f"incident/{incident_id}"
    async with _worktree_lock:
        await _git(settings.repo_dir, "worktree", "add", "-b", branch, str(path), settings.base_ref)
    base_commit = await _git(path, "rev-parse", "HEAD")
    return {"path": str(path), "branch": branch, "base_commit": base_commit}


async def describe_fix(worktree):
    """Return the commits the assistant added on the incident branch, and save nothing else."""
    log = await _git(worktree["path"], "log", "--format=%H %s", f"{worktree['base_commit']}..HEAD")
    commits = [line.split(" ", 1) for line in log.splitlines()]
    diff = await _git(worktree["path"], "diff", worktree["base_commit"], "HEAD") if commits else ""
    return {"commits": [{"sha": sha, "subject": subject} for sha, subject in commits]}, diff
