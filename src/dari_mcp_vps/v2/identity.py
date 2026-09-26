from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

OWNER_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
REPO_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class RepositoryIdentityError(ValueError):
    """Base exception for repository identity validation errors."""


class InvalidRepositoryFormatError(RepositoryIdentityError):
    """Raised when repository string does not conform to canonical owner/name format."""


class OwnerNotAllowedError(RepositoryIdentityError):
    """Raised when repository owner is not in the explicitly allowed owner set."""


class InstallationNotAllowedError(RepositoryIdentityError):
    """Raised when GitHub App installation ID is not in the explicitly allowed installation set."""


@dataclass(frozen=True)
class RepositoryIdentity:
    """Canonical representation of a GitHub repository (owner/name)."""

    owner: str
    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.owner, str) or not isinstance(self.name, str):
            raise InvalidRepositoryFormatError("Repository owner and name must be strings")
        clean_owner, clean_name = self.owner.strip(), self.name.strip()
        if not clean_owner or not clean_name:
            raise InvalidRepositoryFormatError("Repository owner and name cannot be empty")
        for val, f_name in ((clean_owner, "owner"), (clean_name, "name")):
            if "/" in val or "\\" in val or "\x00" in val:
                raise InvalidRepositoryFormatError(f"Prohibited separator or null byte in {f_name}: {val!r}")
            if ".." in val or val in {".", ".."}:
                raise InvalidRepositoryFormatError(f"Path traversal sequence forbidden in {f_name}: {val!r}")
        if not OWNER_PATTERN.fullmatch(clean_owner):
            raise InvalidRepositoryFormatError(f"Invalid GitHub owner format: {clean_owner!r}")
        if not REPO_NAME_PATTERN.fullmatch(clean_name):
            raise InvalidRepositoryFormatError(f"Invalid GitHub repository name format: {clean_name!r}")
        if clean_name.lower().endswith(".git"):
            raise InvalidRepositoryFormatError(f"Repository name must not end with '.git': {clean_name!r}")
        object.__setattr__(self, "owner", clean_owner)
        object.__setattr__(self, "name", clean_name)

    @property
    def canonical(self) -> str:
        return f"{self.owner}/{self.name}"

    def __str__(self) -> str:
        return self.canonical

    def __repr__(self) -> str:
        return f"RepositoryIdentity(owner={self.owner!r}, name={self.name!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RepositoryIdentity):
            return NotImplemented
        return self.owner.lower() == other.owner.lower() and self.name.lower() == other.name.lower()

    def __hash__(self) -> int:
        return hash((self.owner.lower(), self.name.lower()))

    @classmethod
    def from_string(cls, value: str) -> RepositoryIdentity:
        if not isinstance(value, str):
            raise InvalidRepositoryFormatError(f"Expected repository string, got {type(value).__name__}")
        raw = value.strip()
        if not raw:
            raise InvalidRepositoryFormatError("Repository string cannot be empty")
        parts = raw.split("/")
        if len(parts) != 2:
            raise InvalidRepositoryFormatError(f"Invalid repository format {raw!r}. Expected exactly 'owner/name'.")
        return cls(owner=parts[0], name=parts[1])

    def validate_owner(self, allowed_owners: Iterable[str]) -> None:
        if not allowed_owners:
            raise OwnerNotAllowedError("No allowed owners configured; repository access rejected")
        norm = {o.strip().lower() for o in allowed_owners if o.strip()}
        if not norm or self.owner.lower() not in norm:
            raise OwnerNotAllowedError(f"Repository owner {self.owner!r} is not authorized")

    def validate_installation(self, installation_id: int | str, allowed_installations: Iterable[int | str]) -> None:
        if not allowed_installations:
            raise InstallationNotAllowedError("No allowed installations configured; installation access rejected")
        allowed = {str(i).strip() for i in allowed_installations if str(i).strip()}
        if not allowed or installation_id is None:
            raise InstallationNotAllowedError("Installation access rejected")
        target = str(installation_id).strip()
        if not target or target not in allowed:
            raise InstallationNotAllowedError(f"GitHub App installation ID {installation_id!r} is not authorized")

    def validate(
        self,
        allowed_owners: Iterable[str],
        installation_id: int | str | None = None,
        allowed_installations: Iterable[int | str] | None = None,
        require_installation: bool = False,
    ) -> RepositoryIdentity:
        self.validate_owner(allowed_owners)
        if require_installation or installation_id is not None or allowed_installations is not None:
            if installation_id is None or not str(installation_id).strip():
                raise InstallationNotAllowedError("Installation ID is required")
            if allowed_installations is None:
                raise InstallationNotAllowedError("No allowed installations configured")
            self.validate_installation(installation_id, allowed_installations)
        return self


def parse_repository_identity(value: str) -> RepositoryIdentity:
    return RepositoryIdentity.from_string(value)


def validate_repository_identity(
    repo: str | RepositoryIdentity,
    allowed_owners: Iterable[str],
    installation_id: int | str | None = None,
    allowed_installations: Iterable[int | str] | None = None,
    require_installation: bool = False,
) -> RepositoryIdentity:
    identity = repo if isinstance(repo, RepositoryIdentity) else RepositoryIdentity.from_string(repo)
    return identity.validate(
        allowed_owners=allowed_owners,
        installation_id=installation_id,
        allowed_installations=allowed_installations,
        require_installation=require_installation,
    )
