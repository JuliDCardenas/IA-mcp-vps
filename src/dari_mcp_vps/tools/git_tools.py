from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from dari_mcp_vps.security import resolve_allowed_path


def _run_git(repo: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout.strip()


def register_git_tools(mcp: Any, app_config: Any) -> None:
    @mcp.tool()
    def git_status(scope: str, path: str = ".") -> dict[str, Any]:
        """Return read-only git status for an allowlisted repository path."""
        repo = resolve_allowed_path(app_config.raw, scope, path)
        if not repo.is_dir():
            raise NotADirectoryError(str(repo))
        inside = _run_git(repo, ["rev-parse", "--is-inside-work-tree"])
        if inside != "true":
            raise RuntimeError("Path is not inside a git work tree")
        root = _run_git(repo, ["rev-parse", "--show-toplevel"])
        branch = _run_git(repo, ["branch", "--show-current"])
        head = _run_git(repo, ["rev-parse", "HEAD"])
        short_head = _run_git(repo, ["rev-parse", "--short", "HEAD"])
        upstream = None
        ahead_behind = None
        try:
            upstream = _run_git(repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
            counts = _run_git(repo, ["rev-list", "--left-right", "--count", "HEAD...@{u}"])
            ahead, behind = counts.split()
            ahead_behind = {"ahead": int(ahead), "behind": int(behind)}
        except Exception:
            upstream = None
            ahead_behind = None
        porcelain = _run_git(repo, ["status", "--porcelain=v1", "-b"])
        changed = [line for line in porcelain.splitlines() if line and not line.startswith("##")]
        return {
            "root": root,
            "branch": branch,
            "head": head,
            "short_head": short_head,
            "upstream": upstream,
            "ahead_behind": ahead_behind,
            "clean": len(changed) == 0,
            "changed_count": len(changed),
            "status_porcelain": porcelain,
        }
