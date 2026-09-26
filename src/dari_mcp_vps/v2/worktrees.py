from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dari_mcp_vps.v2.discovery import GitHubRepositoryCatalog
from dari_mcp_vps.v2.policy import RepositoryPolicyRegistry
from dari_mcp_vps.v2.token_broker import InstallationToken, InstallationTokenBroker
from dari_mcp_vps.worktree_manager import (
    TIMEOUT_GIT_SECONDS,
    NonFastForwardError,
    RepositoryPolicy,
    SecurityError,
    WorktreeManager,
    WorktreeManagerError,
    _atomic_write_json,
    _run_git,
    _sanitize_id,
    _sanitize_output,
)


@dataclass(frozen=True)
class SelectedRepository:
    canonical: str
    installation_id: int
    runtime_alias: str
    clone_url: str


class GitHubNativeWorktreeManager(WorktreeManager):
    """Activates authorized repositories without changing hardcoded policy maps."""

    def __init__(
        self,
        storage_root: Path,
        policy_registry: RepositoryPolicyRegistry,
        token_broker: InstallationTokenBroker,
        policies: dict[str, RepositoryPolicy] | None = None,
        repository_catalog: GitHubRepositoryCatalog | None = None,
    ) -> None:
        super().__init__(storage_root=storage_root, policies=policies)
        self.policy_registry = policy_registry
        self.token_broker = token_broker
        self.repository_catalog = repository_catalog
        self._selected: dict[str, SelectedRepository] = {}
        self._askpass_path = self._ensure_askpass()

    def _ensure_askpass(self) -> Path:
        target = self._ensure_confinement(self.storage_root / ".trusted-git-askpass")
        content = (
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  Username*) printf '%s\\n' \"$IA_GIT_USERNAME\" ;;\n"
            "  *) printf '%s\\n' \"$IA_GIT_PASSWORD\" ;;\n"
            "esac\n"
        )
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            target.write_text(content, encoding="utf-8")
        target.chmod(0o700)
        return target

    @staticmethod
    def _runtime_alias(canonical: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9_-]+", "_", canonical).strip("_")[:42]
        digest = hashlib.sha256(canonical.lower().encode("utf-8")).hexdigest()[:10]
        return _sanitize_id(f"gh_{slug}_{digest}", "repository_alias")

    def select_repository(
        self,
        repository: str,
        installation_id: int | str,
    ) -> SelectedRepository:
        policy = self.policy_registry.get_policy(repository, installation_id=installation_id)
        discovered = (
            self.repository_catalog.select_repository(repository, installation_id)
            if self.repository_catalog is not None
            else None
        )
        alias = self._runtime_alias(policy.identity.canonical)
        clone_url = f"https://github.com/{policy.identity.canonical}.git"
        selected = SelectedRepository(
            canonical=policy.identity.canonical,
            installation_id=int(installation_id),
            runtime_alias=alias,
            clone_url=clone_url,
        )
        self.policies[alias] = RepositoryPolicy(
            alias=alias,
            clone_url=clone_url,
            default_base_branch=(
                discovered.default_branch if discovered is not None else policy.default_base_branch
            ),
            github_repo=policy.identity.canonical,
            test_commands=policy.test_commands if policy.allow_tests else (),
        )
        self._selected[alias] = selected
        _atomic_write_json(
            self.metadata_dir / f"repository_{alias}.json",
            {
                "canonical": selected.canonical,
                "installation_id": selected.installation_id,
                "runtime_alias": selected.runtime_alias,
                "clone_url": selected.clone_url,
            },
        )
        return selected

    def repository_context(self, alias: str) -> SelectedRepository | None:
        return self._selected.get(alias)

    def _trusted_git_env(self, token: InstallationToken) -> dict[str, str]:
        env = os.environ.copy()
        for key in ("GITHUB_TOKEN", "GH_TOKEN", "GIT_SSH_COMMAND", "SSH_AUTH_SOCK"):
            env.pop(key, None)
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": str(self._askpass_path),
                "IA_GIT_USERNAME": "x-access-token",
                "IA_GIT_PASSWORD": token.expose_secret(),
            }
        )
        return env

    def _run_authenticated_git(
        self,
        args: list[str],
        selected: SelectedRepository,
        cwd: Path | None = None,
    ) -> str:
        token = self.token_broker.get_installation_token(
            selected.installation_id,
            repositories=[selected.canonical.split("/", 1)[1]],
        )
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=cwd,
                env=self._trusted_git_env(token),
                text=True,
                capture_output=True,
                timeout=TIMEOUT_GIT_SECONDS,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorktreeManagerError("Authenticated Git operation timed out") from exc
        if proc.returncode != 0:
            raise WorktreeManagerError(
                f"Authenticated Git operation failed: {_sanitize_output(proc.stderr or proc.stdout)}"
            )
        return proc.stdout.strip()

    def ensure_base_repository(self, alias: str) -> Path:
        selected = self._selected.get(alias)
        if selected is None:
            return super().ensure_base_repository(alias)
        base_path = self._base_repo_path(alias)
        if base_path.exists():
            if not (base_path / "HEAD").exists():
                raise WorktreeManagerError(f"Corrupt base repository detected at {base_path}")
            return base_path
        base_path.parent.mkdir(parents=True, exist_ok=True)
        self._run_authenticated_git(
            ["clone", "--bare", selected.clone_url, str(base_path)],
            selected,
        )
        return base_path

    def refresh_base_repository(
        self,
        alias: str,
        base_branch: str | None = None,
    ) -> tuple[str, str]:
        selected = self._selected.get(alias)
        if selected is None:
            return super().refresh_base_repository(alias, base_branch)
        policy = self.get_policy(alias)
        base_path = self.ensure_base_repository(alias)
        target = _sanitize_id(base_branch or policy.default_base_branch, "base_branch")
        self._run_authenticated_git(
            ["fetch", "origin", f"+refs/heads/{target}:refs/remotes/origin/{target}"],
            selected,
            cwd=base_path,
        )
        new_commit = _run_git(["rev-parse", f"refs/remotes/origin/{target}"], cwd=base_path)
        local = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{target}"],
            cwd=base_path,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_GIT_SECONDS,
            check=False,
        )
        if local.returncode != 0:
            _run_git(["update-ref", f"refs/heads/{target}", new_commit], cwd=base_path)
            return new_commit, new_commit
        old_commit = local.stdout.strip()
        if old_commit == new_commit:
            return old_commit, new_commit
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", old_commit, new_commit],
            cwd=base_path,
            capture_output=True,
            timeout=TIMEOUT_GIT_SECONDS,
            check=False,
        )
        if ancestor.returncode != 0:
            raise NonFastForwardError(
                f"Base branch {target} has diverged remotely; non-fast-forward update rejected."
            )
        _run_git(["update-ref", f"refs/heads/{target}", new_commit, old_commit], cwd=base_path)
        return old_commit, new_commit
