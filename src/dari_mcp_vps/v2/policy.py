from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from dari_mcp_vps.v2.identity import (
    InstallationNotAllowedError,
    OwnerNotAllowedError,
    RepositoryIdentity,
    parse_repository_identity,
)
from dari_mcp_vps.worktree_manager import (
    DEFAULT_REPOSITORY_POLICIES,
    RepositoryPolicy,
)


@dataclass(frozen=True)
class RepositoryPolicyV2:
    identity: RepositoryIdentity
    default_base_branch: str = "main"
    allow_promotion: bool = False
    allow_tests: bool = False
    test_commands: tuple[tuple[str, ...], ...] = ()
    is_conservative_default: bool = False
    alias: str = ""
    clone_url: str = ""
    installation_id: int | str | None = None

    def __post_init__(self) -> None:
        if not self.alias:
            object.__setattr__(self, "alias", self.identity.name)
        if not self.clone_url:
            object.__setattr__(self, "clone_url", f"https://github.com/{self.identity.canonical}.git")

    @property
    def github_repo(self) -> str:
        return self.identity.canonical

    @property
    def owner_repo(self) -> str:
        return self.identity.canonical

    def to_v1_policy(self) -> RepositoryPolicy:
        return RepositoryPolicy(
            alias=self.alias,
            clone_url=self.clone_url,
            default_base_branch=self.default_base_branch,
            github_repo=self.identity.canonical,
            test_commands=self.test_commands,
        )


def build_conservative_policy(
    identity: RepositoryIdentity,
    default_base_branch: str = "main",
    clone_url: str | None = None,
) -> RepositoryPolicyV2:
    return RepositoryPolicyV2(
        identity=identity,
        default_base_branch=default_base_branch,
        allow_promotion=False,
        allow_tests=False,
        test_commands=(),
        is_conservative_default=True,
        alias=identity.name,
        clone_url=clone_url or f"https://github.com/{identity.canonical}.git",
    )


class RepositoryPolicyRegistry:
    def __init__(
        self,
        allowed_owners: Iterable[str] | None = None,
        allowed_installations: Iterable[int | str] | None = None,
        config_source: Any = None,
        fallback_v1: dict[str, RepositoryPolicy] | None = None,
    ) -> None:
        self._initial_allowed_owners = {o.strip().lower() for o in allowed_owners if o.strip()} if allowed_owners else set()
        self._initial_allowed_installations = {str(i).strip() for i in allowed_installations if str(i).strip()} if allowed_installations else set()
        self.allowed_owners = set(self._initial_allowed_owners)
        self.allowed_installations = set(self._initial_allowed_installations)
        self._config_source = config_source
        self._fallback_v1 = dict(fallback_v1 if fallback_v1 is not None else DEFAULT_REPOSITORY_POLICIES)
        self._v1_aliases: set[str] = set(self._fallback_v1.keys())
        self._policies_by_canonical: dict[str, RepositoryPolicyV2] = {}
        self._policies_by_alias: dict[str, RepositoryPolicyV2] = {}

        for v1_pol in self._fallback_v1.values():
            self._register_v1_policy(v1_pol)
        if self._config_source is not None:
            self.reload()

    def _register_v1_policy(
        self,
        v1: RepositoryPolicy,
        target_canonical: dict[str, RepositoryPolicyV2] | None = None,
        target_alias: dict[str, RepositoryPolicyV2] | None = None,
    ) -> None:
        owner_name = v1.github_repo.strip() if v1.github_repo else f"local/{v1.alias}"
        try:
            identity = parse_repository_identity(owner_name)
        except Exception:
            identity = RepositoryIdentity(owner="local", name=v1.alias)
        v2_pol = RepositoryPolicyV2(
            identity=identity,
            default_base_branch=v1.default_base_branch,
            allow_promotion=False,
            allow_tests=bool(v1.test_commands),
            test_commands=v1.test_commands,
            is_conservative_default=False,
            alias=v1.alias,
            clone_url=v1.clone_url,
        )
        canon = target_canonical if target_canonical is not None else self._policies_by_canonical
        alias = target_alias if target_alias is not None else self._policies_by_alias
        canon[identity.canonical.lower()] = v2_pol
        alias[v1.alias] = v2_pol

    def register_policy(self, policy: RepositoryPolicyV2 | RepositoryPolicy) -> None:
        if isinstance(policy, RepositoryPolicy):
            self._register_v1_policy(policy)
            return
        self._policies_by_canonical[policy.identity.canonical.lower()] = policy
        if policy.alias:
            self._policies_by_alias[policy.alias] = policy

    def _parse_config(
        self, data: dict[str, Any]
    ) -> tuple[set[str], set[str], dict[str, RepositoryPolicyV2], dict[str, RepositoryPolicyV2]]:
        repo_section = data.get("repositories") or data.get("orchestrator", {}).get("repositories") or {}
        cfg_owners = repo_section.get("allowed_owners") or data.get("allowed_owners")
        new_owners = {str(o).strip().lower() for o in cfg_owners if str(o).strip()} if isinstance(cfg_owners, (list, tuple, set)) else set(self._initial_allowed_owners)
        cfg_insts = repo_section.get("allowed_installations") or data.get("allowed_installations")
        new_insts = {str(i).strip() for i in cfg_insts if str(i).strip()} if isinstance(cfg_insts, (list, tuple, set)) else set(self._initial_allowed_installations)

        new_by_canonical: dict[str, RepositoryPolicyV2] = {}
        new_by_alias: dict[str, RepositoryPolicyV2] = {}
        for v1_pol in self._fallback_v1.values():
            self._register_v1_policy(v1_pol, target_canonical=new_by_canonical, target_alias=new_by_alias)

        raw_policies = repo_section.get("policies") or data.get("repository_policies") or {}
        if isinstance(raw_policies, dict):
            for key, pol_cfg in raw_policies.items():
                if not isinstance(pol_cfg, dict):
                    continue
                repo_str = str(pol_cfg.get("github_repo") or pol_cfg.get("repo") or key).strip()
                try:
                    identity = parse_repository_identity(repo_str)
                except Exception:
                    continue
                test_cmds = [tuple(str(c) for c in cmd) for cmd in pol_cfg.get("test_commands", ()) if isinstance(cmd, (list, tuple))]
                inst_id = pol_cfg.get("installation_id")
                pol = RepositoryPolicyV2(
                    identity=identity,
                    default_base_branch=str(pol_cfg.get("default_base_branch", "main")),
                    allow_promotion=bool(pol_cfg.get("allow_promotion", False)),
                    allow_tests=bool(pol_cfg.get("allow_tests", bool(test_cmds))),
                    test_commands=tuple(test_cmds),
                    is_conservative_default=False,
                    alias=str(pol_cfg.get("alias") or identity.name),
                    clone_url=str(pol_cfg.get("clone_url") or f"https://github.com/{identity.canonical}.git"),
                    installation_id=str(inst_id).strip() if inst_id is not None else None,
                )
                new_by_canonical[pol.identity.canonical.lower()] = pol
                if pol.alias:
                    new_by_alias[pol.alias] = pol

        return new_owners, new_insts, new_by_canonical, new_by_alias

    def load_from_dict(self, data: dict[str, Any]) -> None:
        new_owners, new_insts, new_canonical, new_alias = self._parse_config(data)
        self.allowed_owners = new_owners
        self.allowed_installations = new_insts
        self._policies_by_canonical = new_canonical
        self._policies_by_alias = new_alias

    def load_from_file(self, path: Path | str) -> None:
        p = Path(path).resolve()
        if p.exists():
            content = p.read_text(encoding="utf-8")
            parsed = yaml.safe_load(content) if content.strip() else {}
            if isinstance(parsed, dict):
                self.load_from_dict(parsed)

    def reload(self) -> None:
        if isinstance(self._config_source, (str, Path)):
            self.load_from_file(self._config_source)
        elif isinstance(self._config_source, dict):
            self.load_from_dict(self._config_source)
        elif callable(self._config_source):
            res = self._config_source()
            if isinstance(res, dict):
                self.load_from_dict(res)

    def get_policy(
        self,
        repo_identifier: str | RepositoryIdentity,
        installation_id: int | str | None = None,
    ) -> RepositoryPolicyV2:
        if isinstance(repo_identifier, str):
            clean_ident = repo_identifier.strip()
            if clean_ident in self._v1_aliases:
                return self._policies_by_alias[clean_ident]

        if not self.allowed_owners:
            raise OwnerNotAllowedError("No allowed owners configured (fail-closed)")

        if isinstance(repo_identifier, RepositoryIdentity):
            identity = repo_identifier
        else:
            ident_str = str(repo_identifier).strip()
            if "/" in ident_str:
                identity = parse_repository_identity(ident_str)
            elif ident_str in self._policies_by_alias and ident_str not in self._v1_aliases:
                identity = self._policies_by_alias[ident_str].identity
            else:
                identity = parse_repository_identity(ident_str)

        identity.validate_owner(self.allowed_owners)

        if not self.allowed_installations:
            raise InstallationNotAllowedError("No allowed installations configured (fail-closed)")
        if installation_id is None or not str(installation_id).strip():
            raise InstallationNotAllowedError("Installation ID is required (fail-closed)")

        inst_str = str(installation_id).strip()
        if inst_str not in self.allowed_installations:
            raise InstallationNotAllowedError(f"GitHub App installation ID {installation_id!r} is not authorized")
        canonical_lower = identity.canonical.lower()
        if canonical_lower in self._policies_by_canonical:
            existing = self._policies_by_canonical[canonical_lower]
            if existing.installation_id is not None and str(existing.installation_id).strip() != inst_str:
                raise InstallationNotAllowedError(
                    f"Installation ID {inst_str} does not match policy installation ID {existing.installation_id}"
                )
            if existing.installation_id is None:
                return RepositoryPolicyV2(
                    identity=existing.identity,
                    default_base_branch=existing.default_base_branch,
                    allow_promotion=existing.allow_promotion,
                    allow_tests=existing.allow_tests,
                    test_commands=existing.test_commands,
                    is_conservative_default=existing.is_conservative_default,
                    alias=existing.alias,
                    clone_url=existing.clone_url,
                    installation_id=inst_str,
                )
            return existing

        conservative = build_conservative_policy(identity)
        return RepositoryPolicyV2(
            identity=conservative.identity,
            default_base_branch=conservative.default_base_branch,
            allow_promotion=False,
            allow_tests=False,
            test_commands=(),
            is_conservative_default=True,
            alias=conservative.alias,
            clone_url=conservative.clone_url,
            installation_id=inst_str,
        )

    def __getitem__(self, key: str) -> RepositoryPolicy:
        return self.get_policy(key).to_v1_policy()

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        clean = key.strip()
        if clean in self._v1_aliases:
            return True
        if not self.allowed_owners or not self.allowed_installations:
            return False
        if clean in self._policies_by_alias or clean.lower() in self._policies_by_canonical:
            return True
        try:
            return parse_repository_identity(clean).owner.lower() in self.allowed_owners
        except Exception:
            return False

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except Exception:
            return default

    def keys(self) -> list[str]:
        combined = set(self._policies_by_alias.keys()) | set(self._policies_by_canonical.keys()) | set(self._fallback_v1.keys())
        return sorted(combined)

    def values(self) -> list[RepositoryPolicy]:
        return [self[k] for k in self.keys()]

    def items(self) -> list[tuple[str, RepositoryPolicy]]:
        return [(k, self[k]) for k in self.keys()]

    def __iter__(self) -> Any:
        return iter(self.keys())

    def __len__(self) -> int:
        return len(self.keys())
