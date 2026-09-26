from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dari_mcp_vps.worktree_manager import TIMEOUT_GIT_SECONDS, SecurityError, WorktreeManagerError

BRANCH_NAME_PATTERN = re.compile(r"^feat/[A-Za-z0-9_-]{1,64}$")


def _redact_sensitive(text: str) -> str:
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)token\s*[:=]\s*['\"]?[A-Za-z0-9._~+/=-]+['\"]?", "token=[REDACTED]", text)
    text = re.sub(r"github_pat_[A-Za-z0-9_]{30,}", "[REDACTED_PAT]", text)
    text = re.sub(r"gh[pousr]_[A-Za-z0-9]{20,}", "[REDACTED_GH_TOKEN]", text)
    return text[:1000]


class PromotionError(WorktreeManagerError):
    """Raised when branch publication or PR creation fails."""


@dataclass(frozen=True)
class PromoterConfig:
    enabled: bool = False
    publish_remote_url: str = ""
    default_base_branch: str = "main"

    @property
    def is_configured(self) -> bool:
        return self.enabled and bool(self.publish_remote_url.strip())


@dataclass(frozen=True)
class PublishResult:
    feature_branch: str
    publish_remote: str
    published_at: str
    success: bool


@dataclass(frozen=True)
class GitHubPRConfig:
    enabled: bool = False
    github_token: str = ""
    owner_repo: str = ""

    @property
    def is_configured(self) -> bool:
        return self.enabled and bool(self.owner_repo.strip())


@dataclass(frozen=True)
class PRResult:
    pr_number: int
    pr_url: str
    status: str
    created_at: str
    feature_branch: str
    base_branch: str


class BranchPromoter:
    """Manages secure publication of approved feature branches."""

    def __init__(self, config: PromoterConfig | None = None) -> None:
        self.config = config or PromoterConfig()

    def publish_branch(
        self,
        base_repo_path: Path,
        feature_branch: str,
        custom_remote: str | None = None,
        expected_commit_sha: str | None = None,
    ) -> PublishResult:
        # 1. Fail closed if promoter is not configured
        remote_url = custom_remote or self.config.publish_remote_url
        if not self.config.is_configured and not custom_remote:
            raise PromotionError(
                "Branch publication is disabled: promoter configuration/credentials are absent (fail-closed)"
            )

        # 2. Strict branch name validation
        if not feature_branch or not BRANCH_NAME_PATTERN.match(feature_branch):
            raise SecurityError(f"Invalid feature branch name for publication: {feature_branch!r}")

        # 3. Destination must never be main or base branch
        if feature_branch == "main" or feature_branch == self.config.default_base_branch:
            raise SecurityError(f"Feature branch cannot match protected base branch: {feature_branch}")

        # 4. Verify branch ref equals expected_commit_sha if specified
        if expected_commit_sha:
            proc_rev = subprocess.run(
                ["git", "-C", str(base_repo_path), "rev-parse", "--verify", f"refs/heads/{feature_branch}"],
                capture_output=True,
                text=True,
                shell=False,
                check=False,
                timeout=TIMEOUT_GIT_SECONDS,
            )
            if proc_rev.returncode != 0:
                raise PromotionError(f"Branch ref refs/heads/{feature_branch} not found in base repository")
            actual_sha = proc_rev.stdout.strip()
            if actual_sha != expected_commit_sha:
                raise PromotionError(
                    f"Branch ref refs/heads/{feature_branch} ({actual_sha}) does not match expected commit {expected_commit_sha}"
                )

        # 5. Push safely without force push
        # Fixed argument array: ["git", "push", remote, f"refs/heads/{branch}:refs/heads/{branch}"]
        refspec = f"refs/heads/{feature_branch}:refs/heads/{feature_branch}"
        push_cmd = ["git", "-C", str(base_repo_path), "push", remote_url, refspec]

        proc = subprocess.run(
            push_cmd,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=TIMEOUT_GIT_SECONDS,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip()[:500] or proc.stdout.strip()[:500]
            raise PromotionError(f"Failed to publish branch {feature_branch}: {err}")

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return PublishResult(
            feature_branch=feature_branch,
            publish_remote=remote_url,
            published_at=now,
            success=True,
        )


class GitHubPRClient:
    """Manages Pull Request creation under a strict promoter boundary."""

    def __init__(self, config: GitHubPRConfig | None = None, http_opener: Any = None) -> None:
        self.config = config or GitHubPRConfig()
        self._http_opener = http_opener or urllib.request.urlopen

    def create_pull_request(
        self,
        owner_repo: str,
        feature_branch: str,
        base_branch: str = "main",
        title: str = "",
        body: str = "",
    ) -> PRResult:
        # 1. Fail closed if GitHub promoter is not configured
        target_repo = (owner_repo or self.config.owner_repo).strip()
        if not self.config.is_configured or not target_repo:
            raise PromotionError(
                "PR creation is disabled: GitHub promoter configuration is absent (fail-closed)"
            )

        if not self.config.github_token.strip():
            raise PromotionError(
                "PR creation is disabled: GitHub credentials absent (fail-closed)"
            )

        # 2. Strict branch name validation
        if not feature_branch or not BRANCH_NAME_PATTERN.match(feature_branch):
            raise SecurityError(f"Invalid feature branch for PR: {feature_branch!r}")

        if feature_branch == "main" or feature_branch == base_branch:
            raise SecurityError("Feature branch cannot match main or base branch for PR creation")

        # 3. Validate owner_repo format (owner/repo)
        if not re.fullmatch(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", target_repo):
            raise SecurityError(f"Invalid GitHub repository identifier: {target_repo!r}")

        # 4. Construct request payload and execute bounded HTTPS request
        api_url = f"https://api.github.com/repos/{target_repo}/pulls"
        payload = {
            "title": title[:256] if title else f"Autonomous PR: {feature_branch}",
            "head": feature_branch,
            "base": base_branch,
            "body": body[:65536] if body else "",
        }
        data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            api_url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.config.github_token.strip()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "IA-MCP-VPS-Promoter",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with self._http_opener(req, timeout=30) as resp:
                raw_bytes = resp.read(65536)
        except urllib.error.HTTPError as exc:
            err_body = exc.read(4096).decode("utf-8", errors="replace")
            redacted_err = _redact_sensitive(err_body)
            raise PromotionError(f"GitHub API error (HTTP {exc.code}): {redacted_err}") from None
        except urllib.error.URLError as exc:
            clean_reason = _redact_sensitive(str(exc.reason))
            raise PromotionError(f"GitHub network connection error: {clean_reason}") from None
        except PromotionError:
            raise
        except Exception as exc:
            clean_err = _redact_sensitive(str(exc))
            raise PromotionError(f"Unexpected error communicating with GitHub: {clean_err}") from None

        try:
            resp_data = json.loads(raw_bytes.decode("utf-8"))
            pr_number = int(resp_data["number"])
            pr_url = str(resp_data["html_url"])
            state = str(resp_data.get("state", "open")).upper()
            created_at = str(
                resp_data.get("created_at") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            )
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise PromotionError(f"Invalid response schema from GitHub API: {exc}") from None

        return PRResult(
            pr_number=pr_number,
            pr_url=pr_url,
            status=state,
            created_at=created_at,
            feature_branch=feature_branch,
            base_branch=base_branch,
        )
