from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dari_mcp_vps.worktree_manager import TIMEOUT_GIT_SECONDS, SecurityError, WorktreeManagerError

ALLOWED_EXTENSIONS = {
    ".py",
    ".md",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".sh",
    ".txt",
    ".html",
    ".css",
    ".js",
    ".ts",
}
ALLOWED_FILENAMES = {"Dockerfile"}

MAX_CHANGED_FILES = 20
MAX_FILE_BYTES = 65536
MAX_TOTAL_BYTES = 524288
MAX_DIFF_LINES = 2000

SECRET_PATTERNS = [
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}", re.IGNORECASE),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}", re.IGNORECASE),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE),
    re.compile(r"(?:api[_-]?key|token|secret|password)\s*[:=]\s*['\"][A-Za-z0-9._~+/=-]{16,}['\"]", re.IGNORECASE),
    re.compile(r"(?:AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}"),
]


class ValidationError(WorktreeManagerError):
    """Raised when deterministic validation fails."""


class EmptyImplementationError(ValidationError):
    """Raised when an implementation job produces zero changed files."""


class SecretDetectedError(ValidationError):
    """Raised when sensitive credentials or secret patterns are discovered."""


class BaseCommitMismatchError(ValidationError):
    """Raised when expected base commit does not match current base commit."""


@dataclass(frozen=True)
class TestCommandResult:
    command: tuple[str, ...]
    exit_code: int
    duration_ms: int
    passed: bool
    stdout_tail: str
    stderr_tail: str


@dataclass(frozen=True)
class FileChangeRecord:
    path: str
    operation: str  # "upsert" or "delete"
    size_bytes: int
    sha256: str | None


@dataclass(frozen=True)
class ValidationReport:
    report_hash: str
    base_commit: str
    feature_branch: str
    diff_sha256: str
    changes: list[FileChangeRecord]
    test_results: list[TestCommandResult]
    passed: bool
    errors: list[str] = field(default_factory=list)


def _redact_secrets(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted


def _scan_for_secrets(text: str, filename: str) -> None:
    for pattern in SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            raise SecretDetectedError(f"Potential secret detected in file {filename}: pattern match {pattern.pattern[:30]}...")


class JobValidator:
    """Performs deterministic, independent validation of worktrees and code changes."""

    def __init__(self, worktree_dir: Path, storage_root: Path) -> None:
        self.worktree_dir = worktree_dir.resolve()
        self.storage_root = storage_root.resolve()
        self._ensure_confinement()

    def _ensure_confinement(self) -> None:
        try:
            self.worktree_dir.relative_to(self.storage_root)
        except ValueError as exc:
            raise SecurityError(f"Worktree path escapes storage root: {self.worktree_dir}") from exc
        if os.path.islink(self.worktree_dir):
            raise SecurityError(f"Symlinks forbidden for worktree root: {self.worktree_dir}")

    @staticmethod
    def build_test_environment() -> dict[str, str]:
        """Construct a sanitized runtime environment for allowlisted test execution.

        Includes fixed deterministic non-secret Git identity so tests can execute
        git commit commands without requiring global or system Git configuration,
        while preserving strictly necessary runtime variables and withholding
        publication credentials or secrets.
        """
        env: dict[str, str] = {
            "GIT_AUTHOR_NAME": "IA MCP Test",
            "GIT_AUTHOR_EMAIL": "ia-mcp-test@localhost",
            "GIT_COMMITTER_NAME": "IA MCP Test",
            "GIT_COMMITTER_EMAIL": "ia-mcp-test@localhost",
        }
        preserved_keys = (
            "PATH",
            "PYTHONPATH",
            "HOME",
            "TMPDIR",
            "TEMP",
            "TMP",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
        )
        for key in preserved_keys:
            val = os.environ.get(key)
            if val is not None:
                env[key] = val

        if "PATH" not in env:
            env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

        return env

    def validate(
        self,
        base_commit: str,
        feature_branch: str,
        test_commands: tuple[tuple[str, ...], ...] = (),
        expected_base_commit: str | None = None,
        task_type: str = "implement",
    ) -> ValidationReport:
        errors: list[str] = []

        # 1. Base commit validation
        if expected_base_commit and expected_base_commit != base_commit:
            raise BaseCommitMismatchError(
                f"Base commit mismatch: expected {expected_base_commit}, actual {base_commit}"
            )

        # 2. Feature branch validation: never main or empty
        if not feature_branch or feature_branch == "main":
            raise ValidationError(f"Invalid feature branch: {feature_branch!r} (cannot be main)")

        # 3. Discover changes relative to base_commit covering live filesystem state
        # (including staged, unstaged, deleted, and untracked files)
        diff_status = subprocess.run(
            ["git", "-C", str(self.worktree_dir), "diff", "--name-status", base_commit],
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=TIMEOUT_GIT_SECONDS,
        )
        if diff_status.returncode != 0:
            raise ValidationError(
                f"git diff failed against base commit {base_commit}: {diff_status.stderr.strip()[:500]}"
            )

        status_run = subprocess.run(
            ["git", "-C", str(self.worktree_dir), "status", "--porcelain=v1", "-uall"],
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=TIMEOUT_GIT_SECONDS,
        )
        if status_run.returncode != 0:
            raise ValidationError(f"git status failed: {status_run.stderr.strip()[:500]}")

        untracked_files: list[str] = []
        tracked_changes: dict[str, str] = {}

        # Parse untracked files and working tree deletions from porcelain status
        for line in status_run.stdout.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            if line_str.startswith("?? "):
                rel_path = line_str[3:].strip().strip('"')
                untracked_files.append(rel_path)
            elif len(line) >= 4 and line[0:2] in {" D", "D ", "MD"}:
                deleted_rel = line[3:].strip().strip('"')
                tracked_changes[deleted_rel] = "D"

        # Parse tracked diff changes
        for line in diff_status.stdout.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            parts = line_str.split("\t")
            if len(parts) >= 2:
                status_code = parts[0].strip()
                if status_code.startswith("R"):
                    # Rename: parts[1] is old (deleted), parts[2] is new (added)
                    old_path = parts[1].strip().strip('"')
                    new_path = parts[2].strip().strip('"')
                    tracked_changes[old_path] = "D"
                    tracked_changes[new_path] = "A"
                else:
                    rel_path = parts[1].strip().strip('"')
                    tracked_changes[rel_path] = status_code

        # Parse changes and validate each changed or untracked file
        changes: list[FileChangeRecord] = []
        seen_paths: set[str] = set()
        total_bytes = 0

        all_candidate_paths: list[tuple[str, str]] = []
        for rel_path, code in tracked_changes.items():
            all_candidate_paths.append((rel_path, "delete" if "D" in code else "upsert"))
        for rel_path in untracked_files:
            all_candidate_paths.append((rel_path, "upsert"))

        for rel_path, operation in all_candidate_paths:
            if rel_path in seen_paths:
                continue
            seen_paths.add(rel_path)

            # Validate path confinement
            file_path = (self.worktree_dir / rel_path).resolve()
            try:
                file_path.relative_to(self.worktree_dir)
            except ValueError:
                raise SecurityError(f"Path traversal detected in changed file: {rel_path}")

            if os.path.islink(self.worktree_dir / rel_path):
                raise SecurityError(f"Symlinks are forbidden in worktree: {rel_path}")

            # Validate allowed extension or filename
            ext = Path(rel_path).suffix
            name = Path(rel_path).name
            if ext not in ALLOWED_EXTENSIONS and name not in ALLOWED_FILENAMES:
                raise ValidationError(f"Disallowed file type in changed files: {rel_path}")

            # Determine operation
            if operation == "delete":
                changes.append(FileChangeRecord(path=rel_path, operation="delete", size_bytes=0, sha256=None))
            else:
                if not file_path.exists():
                    continue
                size = file_path.stat().st_size
                if size > MAX_FILE_BYTES:
                    raise ValidationError(f"File {rel_path} exceeds maximum size limit ({size} > {MAX_FILE_BYTES})")
                total_bytes += size
                if total_bytes > MAX_TOTAL_BYTES:
                    raise ValidationError(f"Total changed bytes exceeds limit ({total_bytes} > {MAX_TOTAL_BYTES})")

                content_bytes = file_path.read_bytes()
                file_sha256 = hashlib.sha256(content_bytes).hexdigest()
                content_text = content_bytes.decode("utf-8", errors="replace")

                # Secret scanning
                _scan_for_secrets(content_text, rel_path)

                # Syntax validation
                if ext == ".py":
                    try:
                        ast.parse(content_text, filename=rel_path)
                    except SyntaxError as exc:
                        raise ValidationError(f"Python syntax error in {rel_path}: {exc}") from exc
                elif ext == ".sh":
                    sh_check = subprocess.run(
                        ["bash", "-n", str(file_path)],
                        capture_output=True,
                        text=True,
                        shell=False,
                        check=False,
                        timeout=10,
                    )
                    if sh_check.returncode != 0:
                        raise ValidationError(f"Shell syntax error in {rel_path}: {sh_check.stderr[:500]}")

                changes.append(FileChangeRecord(path=rel_path, operation="upsert", size_bytes=size, sha256=file_sha256))

        if len(changes) > MAX_CHANGED_FILES:
            raise ValidationError(f"Too many changed files: {len(changes)} > {MAX_CHANGED_FILES}")

        # Empty implementation rejection: implementation jobs must produce changed files
        if task_type == "implement" and len(changes) == 0:
            raise EmptyImplementationError(
                "Implementation produced zero changed files: worktree contains no changes relative to base commit"
            )

        # 4. git diff --check (live worktree against base_commit)
        diff_check = subprocess.run(
            ["git", "-C", str(self.worktree_dir), "diff", "--check", base_commit],
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=TIMEOUT_GIT_SECONDS,
        )
        if diff_check.returncode != 0:
            raise ValidationError(f"git diff --check failed:\n{diff_check.stdout[:1000]}")

        # Check untracked files for whitespace / diff errors
        for u_path in untracked_files:
            u_full = (self.worktree_dir / u_path).resolve()
            if u_full.exists():
                u_check = subprocess.run(
                    ["git", "-C", str(self.worktree_dir), "diff", "--no-index", "--check", "/dev/null", u_path],
                    capture_output=True,
                    text=True,
                    shell=False,
                    check=False,
                    timeout=TIMEOUT_GIT_SECONDS,
                )
                if u_check.stdout.strip():
                    raise ValidationError(f"git diff --check failed for untracked file {u_path}:\n{u_check.stdout[:1000]}")

        # 5. Full unified diff & changed lines calculation
        diff_full = subprocess.run(
            ["git", "-C", str(self.worktree_dir), "diff", base_commit],
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=TIMEOUT_GIT_SECONDS,
        )
        combined_diff_parts = [diff_full.stdout]

        # Append unified diff of untracked files
        for u_path in sorted(untracked_files):
            u_full = (self.worktree_dir / u_path).resolve()
            if u_full.exists():
                u_diff = subprocess.run(
                    ["git", "-C", str(self.worktree_dir), "diff", "--no-index", "/dev/null", u_path],
                    capture_output=True,
                    text=True,
                    shell=False,
                    check=False,
                    timeout=TIMEOUT_GIT_SECONDS,
                )
                combined_diff_parts.append(u_diff.stdout)

        combined_diff = "\n".join(combined_diff_parts)
        diff_sha256 = hashlib.sha256(combined_diff.encode("utf-8")).hexdigest()

        # Enforce MAX_DIFF_LINES
        changed_lines_count = sum(
            1 for line in combined_diff.splitlines()
            if (line.startswith("+") or line.startswith("-")) and not line.startswith(("+++", "---"))
        )
        if changed_lines_count > MAX_DIFF_LINES:
            raise ValidationError(f"Total changed diff lines ({changed_lines_count}) exceeds limit of {MAX_DIFF_LINES}")

        # 6. Execute allowlisted test commands
        test_results: list[TestCommandResult] = []
        test_env = self.build_test_environment()
        for cmd in test_commands:
            if not cmd or not isinstance(cmd, (list, tuple)):
                continue
            proc = subprocess.run(
                list(cmd),
                cwd=str(self.worktree_dir),
                env=test_env,
                capture_output=True,
                text=True,
                shell=False,
                check=False,
                timeout=60,
            )
            passed = proc.returncode == 0
            if not passed:
                errors.append(f"Test command failed: {' '.join(cmd)} (exit code {proc.returncode})")
            test_results.append(
                TestCommandResult(
                    command=tuple(cmd),
                    exit_code=proc.returncode,
                    duration_ms=0,
                    passed=passed,
                    stdout_tail=_redact_secrets(proc.stdout[-1000:]),
                    stderr_tail=_redact_secrets(proc.stderr[-1000:]),
                )
            )

        # 7. Compute deterministic report hash
        report_identity = {
            "base_commit": base_commit,
            "feature_branch": feature_branch,
            "diff_sha256": diff_sha256,
            "changes": [asdict(c) for c in sorted(changes, key=lambda x: x.path)],
            "test_results": [
                {"cmd": list(t.command), "exit_code": t.exit_code, "passed": t.passed}
                for t in test_results
            ],
        }
        report_json = json.dumps(report_identity, sort_keys=True)
        report_hash = hashlib.sha256(report_json.encode("utf-8")).hexdigest()

        return ValidationReport(
            report_hash=report_hash,
            base_commit=base_commit,
            feature_branch=feature_branch,
            diff_sha256=diff_sha256,
            changes=changes,
            test_results=test_results,
            passed=len(errors) == 0,
            errors=errors,
        )
