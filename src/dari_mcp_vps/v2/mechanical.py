from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dari_mcp_vps.worktree_manager import SecurityError, WorktreeManagerError

TEXT_EXTENSIONS = {
    ".css", ".html", ".js", ".json", ".md", ".py", ".sh",
    ".toml", ".ts", ".txt", ".yaml", ".yml",
}


@dataclass(frozen=True)
class MechanicalOperationResult:
    operation: str
    path: str
    changed: bool


class ConfinedMechanicalOperations:
    """Known deterministic edits confined to one selected worktree."""

    def __init__(
        self,
        worktree: Path,
        formatter_commands: Mapping[str, tuple[str, ...]] | None = None,
        timeout_seconds: int = 120,
    ) -> None:
        self.worktree = worktree.resolve()
        if not self.worktree.is_dir():
            raise SecurityError("Worktree does not exist")
        self.formatter_commands = dict(formatter_commands or {})
        self.timeout_seconds = timeout_seconds

    def _path(self, relative_path: str) -> Path:
        if not relative_path or relative_path.startswith("/"):
            raise SecurityError("Mechanical operation requires a relative path")
        candidate = self.worktree / relative_path
        if candidate.is_symlink():
            raise SecurityError("Mechanical operations reject symlinks")
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.worktree)
        except ValueError as exc:
            raise SecurityError("Mechanical operation escapes selected worktree") from exc
        if relative_path == ".git" or relative_path.startswith(".git/"):
            raise SecurityError("Mechanical operations cannot modify Git metadata")
        return resolved

    def delete_file(self, relative_path: str) -> MechanicalOperationResult:
        target = self._path(relative_path)
        if not target.exists():
            return MechanicalOperationResult("delete", relative_path, False)
        if not target.is_file():
            raise SecurityError("Delete operation accepts files only")
        target.unlink()
        return MechanicalOperationResult("delete", relative_path, True)

    def normalize_eof(self, relative_path: str) -> MechanicalOperationResult:
        target = self._path(relative_path)
        if target.suffix.lower() not in TEXT_EXTENSIONS or not target.is_file():
            raise SecurityError("EOF normalization accepts existing allowlisted text files only")
        raw = target.read_bytes()
        if b"\x00" in raw:
            raise SecurityError("Binary files cannot be normalized")
        text = raw.decode("utf-8")
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"
        changed = normalized.encode("utf-8") != raw
        if changed:
            target.write_text(normalized, encoding="utf-8", newline="\n")
        return MechanicalOperationResult("normalize_eof", relative_path, changed)

    def format(self, formatter: str) -> MechanicalOperationResult:
        command = self.formatter_commands.get(formatter)
        if not command:
            raise SecurityError(f"Formatter is not allowlisted: {formatter!r}")
        env = {
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }
        proc = subprocess.run(
            list(command),
            cwd=self.worktree,
            env=env,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
            shell=False,
        )
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout).strip()[:1000]
            raise WorktreeManagerError(f"Allowlisted formatter failed: {message}")
        return MechanicalOperationResult("format", formatter, True)
