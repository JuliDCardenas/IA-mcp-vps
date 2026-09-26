from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
BRANCH_NAME_PATTERN = re.compile(r"^feat/[A-Za-z0-9_-]{1,64}$")
TIMEOUT_GIT_SECONDS = 30
MAX_OUTPUT_CHARS = 1000


class WorktreeManagerError(Exception):
    """Base exception for worktree manager errors."""


class SecurityError(WorktreeManagerError):
    """Raised when an operation violates path confinement or input safety."""


class NonFastForwardError(WorktreeManagerError):
    """Raised when remote base branch updates cannot be applied cleanly via fast-forward."""


class DirtyWorktreeError(WorktreeManagerError):
    """Raised when cleanup is attempted on a worktree with uncommitted changes."""


class RepositoryNotFoundError(WorktreeManagerError):
    """Raised when an unknown repository alias is requested."""


@dataclass(frozen=True)
class RepositoryPolicy:
    alias: str
    clone_url: str
    default_base_branch: str = "main"
    github_repo: str = ""
    test_commands: tuple[tuple[str, ...], ...] = ()


DEFAULT_REPOSITORY_POLICIES: dict[str, RepositoryPolicy] = {
    "ia_mcp_vps": RepositoryPolicy(
        alias="ia_mcp_vps",
        clone_url="https://github.com/JuliDCardenas/IA-mcp-vps.git",
        default_base_branch="main",
        github_repo="JuliDCardenas/IA-mcp-vps",
        test_commands=(("python3", "-m", "unittest", "discover", "-s", "tests"),),
    ),
    "repositorio_bd_emision": RepositoryPolicy(
        alias="repositorio_bd_emision",
        clone_url="git@github.com:JuliDCardenas/repositorio-bd-emision.git",
        default_base_branch="main",
        github_repo="JuliDCardenas/repositorio-bd-emision",
        test_commands=(),
    ),
}


@dataclass(frozen=True)
class WorkspaceRecord:
    repository_alias: str
    base_branch: str
    base_commit: str
    feature_branch: str
    worktree_path: str
    work_item_id: str
    creation_timestamp: str
    update_timestamp: str
    lifecycle_state: str
    metadata_file: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sanitize_id(value: str, field_name: str) -> str:
    if not value or not isinstance(value, str):
        raise SecurityError(f"{field_name} must be a non-empty string")
    if not SAFE_ID_PATTERN.match(value) or value in {".", ".."}:
        raise SecurityError(f"Unsafe {field_name}: contains invalid characters or traversal: {value!r}")
    return value


def _sanitize_output(text: str) -> str:
    sanitized = text.strip()
    if len(sanitized) > MAX_OUTPUT_CHARS:
        sanitized = sanitized[:MAX_OUTPUT_CHARS] + "... [truncated]"
    return sanitized


def _run_git(args: list[str], cwd: Path | None = None, timeout: int = TIMEOUT_GIT_SECONDS) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorktreeManagerError(f"Git command timed out after {timeout}s: git {' '.join(args[:2])}") from exc
    except Exception as exc:
        raise WorktreeManagerError(f"Failed to execute git: {exc}") from exc

    if proc.returncode != 0:
        err = _sanitize_output(proc.stderr or proc.stdout)
        raise WorktreeManagerError(f"Git error (exit code {proc.returncode}): {err}")
    return proc.stdout.strip()


def _atomic_write_json(file_path: Path, data: dict[str, Any]) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = file_path.parent / f"{file_path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    serialized = json.dumps(data, indent=2, sort_keys=True)
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, file_path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


class WorktreeManager:
    """Manages permanent bare base clones and persistent feature worktrees."""

    def __init__(
        self,
        storage_root: Path,
        policies: dict[str, RepositoryPolicy] | None = None,
    ) -> None:
        self.storage_root = storage_root.expanduser().resolve()
        if not self.storage_root.exists():
            self.storage_root.mkdir(parents=True, exist_ok=True)
        elif not self.storage_root.is_dir():
            raise SecurityError(f"Storage root is not a directory: {self.storage_root}")

        self.bases_dir = (self.storage_root / "bases").resolve()
        self.worktrees_dir = (self.storage_root / "worktrees").resolve()
        self.metadata_dir = (self.storage_root / "metadata").resolve()

        self._ensure_confinement(self.bases_dir)
        self._ensure_confinement(self.worktrees_dir)
        self._ensure_confinement(self.metadata_dir)

        self.bases_dir.mkdir(parents=True, exist_ok=True)
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_dir.mkdir(parents=True, exist_ok=True)

        self.policies = dict(policies if policies is not None else DEFAULT_REPOSITORY_POLICIES)

    def _ensure_confinement(self, path: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.storage_root)
        except ValueError as exc:
            raise SecurityError(f"Path escapes configured storage root: {resolved}") from exc
        if os.path.islink(path):
            raise SecurityError(f"Symlinks are forbidden in storage hierarchy: {path}")
        return resolved

    def get_policy(self, alias: str) -> RepositoryPolicy:
        clean_alias = _sanitize_id(alias, "repository_alias")
        if clean_alias not in self.policies:
            raise RepositoryNotFoundError(f"Repository alias is not allowlisted: {alias!r}")
        return self.policies[clean_alias]

    def _base_repo_path(self, alias: str) -> Path:
        clean_alias = _sanitize_id(alias, "repository_alias")
        target = self.bases_dir / f"{clean_alias}.git"
        return self._ensure_confinement(target)

    def _worktree_path(self, alias: str, work_item_id: str) -> Path:
        clean_alias = _sanitize_id(alias, "repository_alias")
        clean_id = _sanitize_id(work_item_id, "work_item_id")
        target = self.worktrees_dir / clean_alias / clean_id
        return self._ensure_confinement(target)

    def _metadata_path(self, alias: str, work_item_id: str) -> Path:
        clean_alias = _sanitize_id(alias, "repository_alias")
        clean_id = _sanitize_id(work_item_id, "work_item_id")
        target = self.metadata_dir / f"{clean_alias}_{clean_id}.json"
        return self._ensure_confinement(target)

    def ensure_base_repository(self, alias: str) -> Path:
        """Ensure that the permanent bare base repository exists for the given alias."""
        policy = self.get_policy(alias)
        base_path = self._base_repo_path(policy.alias)

        if base_path.exists():
            if not (base_path / "HEAD").exists():
                raise WorktreeManagerError(f"Corrupt base repository detected at {base_path}")
            return base_path

        base_path.parent.mkdir(parents=True, exist_ok=True)
        _run_git(["clone", "--bare", policy.clone_url, str(base_path)])
        return base_path

    def refresh_base_repository(self, alias: str, base_branch: str | None = None) -> tuple[str, str]:
        """Safely fetch and fast-forward the base repository. Returns (old_commit, new_commit)."""
        policy = self.get_policy(alias)
        base_path = self.ensure_base_repository(policy.alias)
        target_branch = _sanitize_id(base_branch or policy.default_base_branch, "base_branch")

        # Fetch remote base branch into remote tracking ref first
        remote_ref = f"+refs/heads/{target_branch}:refs/remotes/origin/{target_branch}"
        _run_git(["fetch", "origin", remote_ref], cwd=base_path)

        new_commit = _run_git(["rev-parse", f"refs/remotes/origin/{target_branch}"], cwd=base_path)

        # Check if local branch ref exists
        check_local = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{target_branch}"],
            cwd=base_path,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=TIMEOUT_GIT_SECONDS,
        )
        if check_local.returncode != 0:
            # Local ref does not exist yet; initialize it directly to new_commit
            _run_git(["update-ref", f"refs/heads/{target_branch}", new_commit], cwd=base_path)
            return new_commit, new_commit

        old_commit = check_local.stdout.strip()

        if old_commit == new_commit:
            return old_commit, new_commit

        # Validate that old_commit is an ancestor of new_commit (strict fast-forward verification)
        check_ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", old_commit, new_commit],
            cwd=base_path,
            capture_output=True,
            shell=False,
            check=False,
            timeout=TIMEOUT_GIT_SECONDS,
        )
        if check_ancestor.returncode != 0:
            raise NonFastForwardError(
                f"Base branch {target_branch} has diverged remotely; non-fast-forward update rejected."
            )

        # Advance local branch reference cleanly
        _run_git(["update-ref", f"refs/heads/{target_branch}", new_commit, old_commit], cwd=base_path)
        return old_commit, new_commit

    def get_or_create_workspace(
        self,
        alias: str,
        work_item_id: str,
        base_branch: str | None = None,
    ) -> WorkspaceRecord:
        """Create or reuse a persistent worktree for the given work item on a dedicated feature branch."""
        policy = self.get_policy(alias)
        clean_alias = policy.alias
        clean_id = _sanitize_id(work_item_id, "work_item_id")
        target_base = _sanitize_id(base_branch or policy.default_base_branch, "base_branch")

        meta_path = self._metadata_path(clean_alias, clean_id)
        worktree_dir = self._worktree_path(clean_alias, clean_id)

        # Reuse existing worktree if valid and active
        if meta_path.exists():
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    raw_meta = json.load(f)
                if (
                    raw_meta.get("work_item_id") == clean_id
                    and raw_meta.get("repository_alias") == clean_alias
                    and raw_meta.get("lifecycle_state") == "ACTIVE"
                    and worktree_dir.exists()
                ):
                    record = WorkspaceRecord(
                        repository_alias=clean_alias,
                        base_branch=raw_meta["base_branch"],
                        base_commit=raw_meta["base_commit"],
                        feature_branch=raw_meta["feature_branch"],
                        worktree_path=str(worktree_dir),
                        work_item_id=clean_id,
                        creation_timestamp=raw_meta["creation_timestamp"],
                        update_timestamp=_now_iso(),
                        lifecycle_state="ACTIVE",
                        metadata_file=str(meta_path),
                    )
                    _atomic_write_json(meta_path, asdict(record))
                    return record
            except (json.JSONDecodeError, KeyError):
                pass

        # Server-generated feature branch MUST differ from main and base branch
        feature_branch = f"feat/{clean_alias}-{clean_id}"
        if feature_branch == target_base or feature_branch == "main":
            raise SecurityError("Feature branch cannot match main or the configured base branch")

        if worktree_dir.exists():
            if os.path.islink(worktree_dir):
                raise SecurityError(f"Symlinks are forbidden at worktree target: {worktree_dir}")
            # If directory exists without active metadata, reject collision
            raise WorktreeManagerError(f"Worktree destination path already exists and cannot be overwritten: {worktree_dir}")

        # When creating a NEW workspace, safely refresh the base repository
        # before reading the base commit and creating the feature branch.
        self.refresh_base_repository(clean_alias, target_base)
        base_path = self._base_repo_path(clean_alias)
        current_base_commit = _run_git(["rev-parse", f"refs/heads/{target_base}"], cwd=base_path)

        worktree_dir.parent.mkdir(parents=True, exist_ok=True)

        # Check if the feature branch already exists in base repo
        branch_check = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{feature_branch}"],
            cwd=base_path,
            capture_output=True,
            shell=False,
            check=False,
            timeout=TIMEOUT_GIT_SECONDS,
        )
        if branch_check.returncode == 0:
            _run_git(["worktree", "add", str(worktree_dir), feature_branch], cwd=base_path)
        else:
            _run_git(["worktree", "add", "-b", feature_branch, str(worktree_dir), current_base_commit], cwd=base_path)

        record = WorkspaceRecord(
            repository_alias=clean_alias,
            base_branch=target_base,
            base_commit=current_base_commit,
            feature_branch=feature_branch,
            worktree_path=str(worktree_dir),
            work_item_id=clean_id,
            creation_timestamp=_now_iso(),
            update_timestamp=_now_iso(),
            lifecycle_state="ACTIVE",
            metadata_file=str(meta_path),
        )
        _atomic_write_json(meta_path, asdict(record))
        return record

    def cleanup_workspace(
        self,
        alias: str,
        work_item_id: str,
        force_discard: bool = False,
    ) -> WorkspaceRecord:
        """Safely clean up a worktree. Rejects dirty worktrees unless force_discard is True."""
        policy = self.get_policy(alias)
        clean_alias = policy.alias
        clean_id = _sanitize_id(work_item_id, "work_item_id")

        meta_path = self._metadata_path(clean_alias, clean_id)
        worktree_dir = self._worktree_path(clean_alias, clean_id)

        if not meta_path.exists():
            raise WorktreeManagerError(f"Workspace metadata not found for {clean_alias}/{clean_id}")

        with open(meta_path, "r", encoding="utf-8") as f:
            raw_meta = json.load(f)

        if raw_meta.get("lifecycle_state") == "CLEANED":
            return WorkspaceRecord(**raw_meta)

        base_path = self.ensure_base_repository(clean_alias)

        if worktree_dir.exists():
            # Check for uncommitted changes (dirty check)
            status_output = _run_git(["status", "--porcelain=v1"], cwd=worktree_dir)
            if status_output and not force_discard:
                raise DirtyWorktreeError(
                    f"Refusing to clean up dirty worktree for {clean_id}: working tree has uncommitted modifications."
                )

            if force_discard:
                _run_git(["worktree", "remove", "--force", str(worktree_dir)], cwd=base_path)
            else:
                _run_git(["worktree", "remove", str(worktree_dir)], cwd=base_path)
            _run_git(["worktree", "prune"], cwd=base_path)

        updated_record = WorkspaceRecord(
            repository_alias=clean_alias,
            base_branch=raw_meta["base_branch"],
            base_commit=raw_meta["base_commit"],
            feature_branch=raw_meta["feature_branch"],
            worktree_path=str(worktree_dir),
            work_item_id=clean_id,
            creation_timestamp=raw_meta["creation_timestamp"],
            update_timestamp=_now_iso(),
            lifecycle_state="CLEANED",
            metadata_file=str(meta_path),
        )
        _atomic_write_json(meta_path, asdict(updated_record))
        return updated_record

    def get_branch_commit(self, alias: str, branch: str) -> str:
        """Return the commit SHA for a branch in the base repository."""
        policy = self.get_policy(alias)
        if branch != "main" and branch != policy.default_base_branch and not BRANCH_NAME_PATTERN.match(branch):
            raise SecurityError(f"Invalid branch name: {branch!r}")
        base_path = self.ensure_base_repository(policy.alias)
        return _run_git(["rev-parse", "--verify", f"refs/heads/{branch}"], cwd=base_path)

    def commit_workspace(
        self,
        alias: str,
        work_item_id: str,
        feature_branch: str,
        base_branch: str,
        changes: list[Any],
        goal: str = "",
    ) -> str:
        """Stage already validated changes and create exactly one local commit on feature branch.

        Returns the approved commit SHA.
        """
        policy = self.get_policy(alias)
        clean_alias = policy.alias
        clean_id = _sanitize_id(work_item_id, "work_item_id")
        clean_base = _sanitize_id(base_branch, "base_branch")

        if not BRANCH_NAME_PATTERN.match(feature_branch):
            raise SecurityError(f"Invalid feature branch: {feature_branch!r}")

        wt_path = self._worktree_path(clean_alias, clean_id)
        if not wt_path.exists():
            raise WorktreeManagerError(f"Worktree path does not exist: {wt_path}")

        # Verify checked-out branch in worktree matches feature_branch and is not main/base
        current_branch = _run_git(["symbolic-ref", "--short", "HEAD"], cwd=wt_path)
        if current_branch != feature_branch:
            raise SecurityError(
                f"Worktree branch mismatch: expected {feature_branch}, found {current_branch}"
            )
        if current_branch == "main" or current_branch == clean_base:
            raise SecurityError(f"Cannot commit on protected base branch {current_branch}")

        # Stage only the already validated worktree changes
        for change in changes:
            rel_path = change.path if hasattr(change, "path") else change["path"]
            op = getattr(change, "operation", None) or (change.get("operation") if isinstance(change, dict) else "upsert")
            if op == "delete":
                _run_git(["rm", "--ignore-unmatch", "--", rel_path], cwd=wt_path)
            else:
                _run_git(["add", "--", rel_path], cwd=wt_path)

        # Deterministic non-secret orchestrator identity
        commit_env = os.environ.copy()
        commit_env["GIT_AUTHOR_NAME"] = "IA MCP Orchestrator"
        commit_env["GIT_AUTHOR_EMAIL"] = "ia-mcp-orchestrator@localhost"
        commit_env["GIT_COMMITTER_NAME"] = "IA MCP Orchestrator"
        commit_env["GIT_COMMITTER_EMAIL"] = "ia-mcp-orchestrator@localhost"
        commit_env.pop("GITHUB_TOKEN", None)
        commit_env.pop("GH_TOKEN", None)
        commit_env.pop("GIT_SSH_COMMAND", None)

        # Bounded server-generated commit message derived from work_item_id or goal
        goal_summary = re.sub(r"\s+", " ", goal.strip())[:60] if goal else "approved changes"
        commit_msg = f"feat({clean_id}): {goal_summary}"[:100]

        check_cached = subprocess.run(
            ["git", "-C", str(wt_path), "diff", "--cached", "--quiet"],
            capture_output=True,
            shell=False,
            check=False,
            timeout=TIMEOUT_GIT_SECONDS,
        )

        if check_cached.returncode != 0:
            proc = subprocess.run(
                ["git", "-C", str(wt_path), "commit", "-m", commit_msg],
                env=commit_env,
                capture_output=True,
                text=True,
                shell=False,
                check=False,
                timeout=TIMEOUT_GIT_SECONDS,
            )
            if proc.returncode != 0:
                raise WorktreeManagerError(f"Failed to create commit: {_sanitize_output(proc.stderr or proc.stdout)}")
        else:
            base_path = self.ensure_base_repository(clean_alias)
            base_commit = _run_git(["rev-parse", f"refs/heads/{clean_base}"], cwd=base_path)
            current_head = _run_git(["rev-parse", "HEAD"], cwd=wt_path)
            if current_head == base_commit:
                proc = subprocess.run(
                    ["git", "-C", str(wt_path), "commit", "--allow-empty", "-m", commit_msg],
                    env=commit_env,
                    capture_output=True,
                    text=True,
                    shell=False,
                    check=False,
                    timeout=TIMEOUT_GIT_SECONDS,
                )
                if proc.returncode != 0:
                    raise WorktreeManagerError(f"Failed to create commit: {_sanitize_output(proc.stderr or proc.stdout)}")

        return _run_git(["rev-parse", "HEAD"], cwd=wt_path)
