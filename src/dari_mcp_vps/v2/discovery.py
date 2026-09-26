from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from dari_mcp_vps.v2.identity import (
    InstallationNotAllowedError,
    OwnerNotAllowedError,
    RepositoryIdentity,
    parse_repository_identity,
)
from dari_mcp_vps.v2.token_broker import InstallationTokenBroker, redact_secrets
from dari_mcp_vps.worktree_manager import RepositoryNotFoundError, WorktreeManagerError


class DiscoveryError(WorktreeManagerError):
    def __init__(self, message: str) -> None:
        super().__init__(redact_secrets(message))


class DiscoveryAuthenticationError(DiscoveryError):
    pass


class DiscoveryPermissionError(DiscoveryError):
    pass


class InstallationNotFoundError(DiscoveryError):
    pass


class DiscoveryResponseError(DiscoveryError):
    pass


def _sanitize_url(url: str) -> str:
    if not url:
        return ""
    return redact_secrets(re.sub(r"https?://[^@/]+@", "https://", url))


@dataclass(frozen=True)
class DiscoveredRepository:
    id: int
    owner: str
    name: str
    full_name: str
    default_branch: str = "main"
    private: bool = True
    clone_url: str = ""
    permissions: dict[str, bool] = field(default_factory=dict)
    archived: bool = False
    disabled: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        expected = f"{self.owner}/{self.name}"
        if self.full_name.lower() != expected.lower():
            object.__setattr__(self, "full_name", expected)
        if self.clone_url:
            object.__setattr__(self, "clone_url", _sanitize_url(self.clone_url))

    @property
    def identity(self) -> RepositoryIdentity:
        return RepositoryIdentity(owner=self.owner, name=self.name)

    @property
    def canonical(self) -> str:
        return f"{self.owner}/{self.name}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["canonical"] = self.canonical
        return data


@dataclass(frozen=True)
class RepositoryDiscoveryPage:
    repositories: list[DiscoveredRepository]
    total_count: int
    page: int
    per_page: int
    has_next_page: bool


class GitHubRepositoryCatalog:
    def __init__(
        self,
        token_broker: InstallationTokenBroker,
        allowed_owners: Iterable[str] | None = None,
        allowed_installations: Iterable[int | str] | None = None,
        http_transport: Any = None,
        api_base_url: str = "https://api.github.com",
    ) -> None:
        self.token_broker = token_broker
        self.allowed_owners = {o.strip().lower() for o in allowed_owners if o.strip()} if allowed_owners else set()
        self.allowed_installations = {str(i).strip() for i in allowed_installations if str(i).strip()} if allowed_installations else set()
        self.http_transport = http_transport or urllib.request.urlopen
        self.api_base_url = api_base_url.rstrip("/")

    def _parse_link_header(self, link_header: str | None) -> bool:
        if not link_header:
            return False
        for part in link_header.split(","):
            segments = part.split(";")
            if len(segments) >= 2 and any('rel="next"' in s.strip().lower() for s in segments[1:]):
                return True
        return False

    def list_repositories(
        self,
        installation_id: int | str,
        page: int = 1,
        per_page: int = 30,
    ) -> RepositoryDiscoveryPage:
        if not self.allowed_installations:
            raise InstallationNotAllowedError("No allowed installations configured (fail-closed)")
        if not self.allowed_owners:
            raise OwnerNotAllowedError("No allowed owners configured (fail-closed)")
        if installation_id is None:
            raise InstallationNotAllowedError("Installation ID cannot be None")
        inst_key = str(installation_id).strip()
        if not inst_key or not inst_key.isdigit():
            raise InstallationNotAllowedError(f"Invalid installation ID: {installation_id!r}")
        if inst_key not in self.allowed_installations:
            raise InstallationNotAllowedError(f"Installation ID {installation_id!r} is not authorized")

        page_num = max(1, int(page))
        per_page_num = min(max(1, int(per_page)), 100)
        token = self.token_broker.get_installation_token(installation_id=int(inst_key))
        endpoint = f"{self.api_base_url}/installation/repositories?page={page_num}&per_page={per_page_num}"
        req = urllib.request.Request(
            endpoint,
            headers={
                "Authorization": f"Bearer {token.expose_secret()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "IA-MCP-VPS-Catalog/2.0",
            },
            method="GET",
        )
        try:
            with self.http_transport(req, timeout=30) as resp:
                raw_bytes = resp.read(262144)
                link_header = resp.headers.get("Link") if hasattr(resp, "headers") else None
        except urllib.error.HTTPError as exc:
            exc.read(4096)
            clean_err = "[REDACTED]"
            if exc.code == 401:
                raise DiscoveryAuthenticationError(f"Discovery unauthorized (HTTP 401): {clean_err}") from None
            if exc.code == 403:
                raise DiscoveryPermissionError(f"Discovery forbidden (HTTP 403): {clean_err}") from None
            if exc.code == 404:
                raise InstallationNotFoundError(f"Installation not found (HTTP 404): {clean_err}") from None
            raise DiscoveryError(f"GitHub API error (HTTP {exc.code}): {clean_err}") from None
        except urllib.error.URLError as exc:
            raise DiscoveryError(f"Network error: {redact_secrets(str(exc.reason))}") from None
        except (DiscoveryError, WorktreeManagerError):
            raise
        except Exception as exc:
            raise DiscoveryError(f"Unexpected discovery error: {redact_secrets(str(exc))}") from None

        try:
            payload = json.loads(raw_bytes.decode("utf-8"))
            total_count = int(payload.get("total_count", 0))
            raw_repos = payload.get("repositories", [])
            if not isinstance(raw_repos, list):
                raise DiscoveryResponseError("Expected 'repositories' array in response")
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise DiscoveryResponseError(f"Malformed JSON response: {exc}") from None

        discovered: list[DiscoveredRepository] = []
        for item in raw_repos:
            if not isinstance(item, dict):
                continue
            repo_name = str(item.get("name", "")).strip()
            owner_data = item.get("owner", {})
            owner_name = (owner_data.get("login") if isinstance(owner_data, dict) else str(item.get("full_name", "")).split("/")[0]) or ""
            owner_name = str(owner_name).strip()
            if not repo_name or not owner_name or owner_name.lower() not in self.allowed_owners:
                continue
            discovered.append(
                DiscoveredRepository(
                    id=int(item.get("id", 0)),
                    owner=owner_name,
                    name=repo_name,
                    full_name=f"{owner_name}/{repo_name}",
                    default_branch=str(item.get("default_branch", "main")),
                    private=bool(item.get("private", True)),
                    clone_url=str(item.get("clone_url", f"https://github.com/{owner_name}/{repo_name}.git")),
                    permissions=dict(item.get("permissions", {})),
                    archived=bool(item.get("archived", False)),
                    disabled=bool(item.get("disabled", False)),
                    description=str(item.get("description") or ""),
                )
            )

        has_next = self._parse_link_header(link_header) if link_header is not None else (page_num * per_page_num) < total_count
        return RepositoryDiscoveryPage(
            repositories=discovered,
            total_count=total_count,
            page=page_num,
            per_page=per_page_num,
            has_next_page=has_next,
        )

    def list_all_repositories(
        self,
        installation_id: int | str,
        max_pages: int = 10,
        per_page: int = 100,
    ) -> list[DiscoveredRepository]:
        all_repos: list[DiscoveredRepository] = []
        page = 1
        while page <= max_pages:
            p = self.list_repositories(installation_id=installation_id, page=page, per_page=per_page)
            all_repos.extend(p.repositories)
            if not p.has_next_page or not p.repositories:
                break
            page += 1
        return all_repos

    def select_repository(
        self,
        canonical_name: str,
        installation_id: int | str,
    ) -> DiscoveredRepository:
        if not self.allowed_installations:
            raise InstallationNotAllowedError("No allowed installations configured (fail-closed)")
        if not self.allowed_owners:
            raise OwnerNotAllowedError("No allowed owners configured (fail-closed)")
        if installation_id is None:
            raise InstallationNotAllowedError("Installation ID cannot be None")
        inst_key = str(installation_id).strip()
        if not inst_key:
            raise InstallationNotAllowedError("Installation ID cannot be empty")
        if inst_key not in self.allowed_installations:
            raise InstallationNotAllowedError(f"Installation ID {installation_id!r} is not authorized")

        identity = parse_repository_identity(canonical_name)
        identity.validate_owner(self.allowed_owners)
        for repo in self.list_all_repositories(installation_id=inst_key):
            if repo.identity == identity:
                return repo
        raise RepositoryNotFoundError(f"Repository '{identity.canonical}' was not found for installation {installation_id}")
