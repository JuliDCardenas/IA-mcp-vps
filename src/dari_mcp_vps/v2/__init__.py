"""V2 GitHub-native orchestrator foundation vertical slice."""
from __future__ import annotations

from dari_mcp_vps.v2.discovery import (
    DiscoveredRepository,
    DiscoveryAuthenticationError,
    DiscoveryError,
    DiscoveryPermissionError,
    DiscoveryResponseError,
    GitHubRepositoryCatalog,
    InstallationNotFoundError,
    RepositoryDiscoveryPage,
)
from dari_mcp_vps.v2.identity import (
    InstallationNotAllowedError,
    InvalidRepositoryFormatError,
    OwnerNotAllowedError,
    RepositoryIdentity,
    RepositoryIdentityError,
    parse_repository_identity,
    validate_repository_identity,
)
from dari_mcp_vps.v2.policy import (
    RepositoryPolicyRegistry,
    RepositoryPolicyV2,
    build_conservative_policy,
)
from dari_mcp_vps.v2.token_broker import (
    GitHubAppTokenBroker,
    InstallationToken,
    InstallationTokenBroker,
    SecretToken,
    StaticInstallationTokenBroker,
    TokenBrokerAuthError,
    TokenBrokerConfigError,
    TokenBrokerError,
    redact_secrets,
)
from dari_mcp_vps.v2.mechanical import (
    ConfinedMechanicalOperations,
    MechanicalOperationResult,
)
from dari_mcp_vps.v2.worktrees import (
    GitHubNativeWorktreeManager,
    SelectedRepository,
)

__all__ = [
    "RepositoryIdentity", "RepositoryIdentityError", "InvalidRepositoryFormatError",
    "OwnerNotAllowedError", "InstallationNotAllowedError", "parse_repository_identity",
    "validate_repository_identity", "SecretToken", "InstallationToken",
    "InstallationTokenBroker", "StaticInstallationTokenBroker", "GitHubAppTokenBroker",
    "TokenBrokerError", "TokenBrokerConfigError", "TokenBrokerAuthError", "redact_secrets",
    "DiscoveredRepository", "RepositoryDiscoveryPage", "GitHubRepositoryCatalog",
    "DiscoveryError", "DiscoveryAuthenticationError", "DiscoveryPermissionError",
    "InstallationNotFoundError", "DiscoveryResponseError", "RepositoryPolicyV2",
    "RepositoryPolicyRegistry", "build_conservative_policy",
    "ConfinedMechanicalOperations", "MechanicalOperationResult",
    "GitHubNativeWorktreeManager", "SelectedRepository",
]
