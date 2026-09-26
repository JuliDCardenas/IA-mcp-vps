from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

REDACTED_STR = "[REDACTED]"
REDACTED_TOKEN_STR = "[REDACTED_INSTALLATION_TOKEN]"

SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)((?:token|secret|password|key)\s*[:=]\s*)['\"]?[A-Za-z0-9._~+/=-]+['\"]?"),
    re.compile(r"gh[s_][A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
]


def redact_secrets(text: str) -> str:
    if not text:
        return ""
    redacted = str(text)
    for pat in SECRET_PATTERNS:
        redacted = pat.sub(r"\1[REDACTED]" if "(" in pat.pattern[:5] else REDACTED_STR, redacted)
    return redacted


class TokenBrokerError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(redact_secrets(message))


class TokenBrokerConfigError(TokenBrokerError):
    pass


class TokenBrokerAuthError(TokenBrokerError):
    pass


class SecretToken:
    __slots__ = ("_secret",)

    def __init__(self, secret: str) -> None:
        if not isinstance(secret, str) or not secret:
            raise ValueError("Secret must be a non-empty string")
        self._secret = secret

    def expose_secret(self) -> str:
        return self._secret

    def __repr__(self) -> str:
        return f"'{REDACTED_STR}'"

    def __str__(self) -> str:
        return REDACTED_STR

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SecretToken) and self._secret == other._secret

    def __hash__(self) -> int:
        return hash(self._secret)


class InstallationToken:
    def __init__(
        self,
        installation_id: int | str,
        secret: str | SecretToken,
        expires_at: datetime | str,
        permissions: dict[str, str] | None = None,
        repositories: list[str] | tuple[str, ...] | None = None,
        token_type: str = "installation",
    ) -> None:
        self.installation_id = int(installation_id)
        self._secret = secret if isinstance(secret, SecretToken) else SecretToken(secret)
        if isinstance(expires_at, datetime):
            self.expires_at = expires_at.astimezone(timezone.utc)
        elif isinstance(expires_at, str):
            clean_ts = expires_at.strip().rstrip("Z")
            parsed = datetime.fromisoformat(clean_ts)
            self.expires_at = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        else:
            raise ValueError("expires_at must be a datetime or ISO timestamp string")
        self.permissions = dict(permissions or {})
        self.repositories = tuple(sorted(repositories)) if repositories is not None else None
        self.token_type = token_type

    def is_expired(self, buffer_seconds: int = 60) -> bool:
        return (datetime.now(timezone.utc) + timedelta(seconds=buffer_seconds)) >= self.expires_at

    @property
    def token_preview(self) -> str:
        return REDACTED_STR

    def expose_secret(self) -> str:
        return self._secret.expose_secret()

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "installation_id": self.installation_id,
            "expires_at": self.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "permissions": dict(self.permissions),
            "repositories": list(self.repositories) if self.repositories is not None else None,
            "token": REDACTED_STR,
            "token_type": self.token_type,
        }

    def __repr__(self) -> str:
        return (
            f"InstallationToken(installation_id={self.installation_id}, "
            f"expires_at={self.expires_at.strftime('%Y-%m-%dT%H:%M:%SZ')!r}, "
            f"token={REDACTED_STR!r})"
        )

    def __str__(self) -> str:
        return REDACTED_TOKEN_STR


class InstallationTokenBroker(ABC):
    @abstractmethod
    def get_installation_token(
        self,
        installation_id: int | str,
        repositories: list[str] | None = None,
    ) -> InstallationToken:
        pass

    def revoke_token(self, token: InstallationToken) -> None:
        pass


class StaticInstallationTokenBroker(InstallationTokenBroker):
    def __init__(
        self,
        token_factory: Callable[[int, tuple[str, ...] | None], tuple[str, datetime]] | None = None,
        default_ttl_seconds: int = 3600,
        fixed_token_value: str = "mock-token",
    ) -> None:
        self._token_factory = token_factory
        self._default_ttl_seconds = default_ttl_seconds
        self._fixed_token_value = fixed_token_value
        self._cache: dict[tuple[str, tuple[str, ...] | None], InstallationToken] = {}
        self.call_count = 0

    def get_installation_token(
        self,
        installation_id: int | str,
        repositories: list[str] | None = None,
    ) -> InstallationToken:
        self.call_count += 1
        inst_key = str(installation_id)
        repo_key = tuple(sorted(repositories)) if repositories is not None else None
        cache_key = (inst_key, repo_key)
        cached = self._cache.get(cache_key)
        if cached is not None and not cached.is_expired(buffer_seconds=30):
            return cached
        if self._token_factory:
            secret_str, expiry = self._token_factory(int(installation_id), repo_key)
        else:
            secret_str = f"{self._fixed_token_value}_{inst_key}_{self.call_count}"
            expiry = datetime.now(timezone.utc) + timedelta(seconds=self._default_ttl_seconds)
        new_token = InstallationToken(installation_id, secret_str, expiry, repositories=repositories)
        self._cache[cache_key] = new_token
        return new_token

    def revoke_token(self, token: InstallationToken) -> None:
        self._cache.pop((str(token.installation_id), token.repositories), None)


class GitHubAppTokenBroker(InstallationTokenBroker):
    def __init__(
        self,
        app_id: int | str | None = None,
        private_key: str | bytes | None = None,
        http_transport: Any = None,
        jwt_signer: Callable[[int | str], str] | None = None,
        api_base_url: str = "https://api.github.com",
        default_ttl_seconds: int = 3600,
    ) -> None:
        self.app_id = str(app_id).strip() if app_id is not None else ""
        self.private_key = private_key
        self.http_transport = http_transport or urllib.request.urlopen
        self.jwt_signer = jwt_signer
        self.api_base_url = api_base_url.rstrip("/")
        self.default_ttl_seconds = default_ttl_seconds
        self._cache: dict[tuple[str, tuple[str, ...] | None], InstallationToken] = {}

    @property
    def is_configured(self) -> bool:
        return bool(self.jwt_signer) or (bool(self.app_id) and self.private_key is not None)

    def _generate_app_jwt(self) -> str:
        if self.jwt_signer:
            return self.jwt_signer(self.app_id)
        if not self.is_configured:
            raise TokenBrokerConfigError("GitHub App credentials (app_id/private_key) are not configured (fail-closed)")
        raise TokenBrokerConfigError("Live RS256 JWT signing requires injectable jwt_signer or cryptographic dependency")

    def get_installation_token(
        self,
        installation_id: int | str,
        repositories: list[str] | None = None,
    ) -> InstallationToken:
        inst_key = str(installation_id).strip()
        if not inst_key or not inst_key.isdigit():
            raise TokenBrokerError(f"Invalid installation ID: {installation_id!r}")
        repo_key = tuple(sorted(repositories)) if repositories is not None else None
        cache_key = (inst_key, repo_key)
        cached = self._cache.get(cache_key)
        if cached is not None and not cached.is_expired(buffer_seconds=120):
            return cached
        if not self.is_configured:
            raise TokenBrokerConfigError("Cannot exchange installation token: GitHub App credentials are not configured (fail-closed)")

        app_jwt = self._generate_app_jwt()
        endpoint = f"{self.api_base_url}/app/installations/{inst_key}/access_tokens"
        payload = {"repositories": list(repositories)} if repositories else {}
        body = json.dumps(payload).encode("utf-8") if payload else None
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "IA-MCP-VPS-TokenBroker/2.0",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self.http_transport(req, timeout=30) as resp:
                raw_bytes = resp.read(65536)
        except urllib.error.HTTPError as exc:
            exc.read(4096)
            clean_err = "[REDACTED]"
            if exc.code in (401, 403):
                raise TokenBrokerAuthError(f"GitHub App token exchange unauthorized (HTTP {exc.code}): {clean_err}") from None
            raise TokenBrokerError(f"GitHub App token exchange failed (HTTP {exc.code}): {clean_err}") from None
        except urllib.error.URLError as exc:
            clean_reason = redact_secrets(str(exc.reason))
            raise TokenBrokerError(f"Network error during token exchange: {clean_reason}") from None
        except TokenBrokerError:
            raise
        except Exception as exc:
            clean_err = redact_secrets(str(exc))
            raise TokenBrokerError(f"Unexpected error during token exchange: {clean_err}") from None

        try:
            data = json.loads(raw_bytes.decode("utf-8"))
            raw_tok = str(data["token"])
            expires_at = str(data["expires_at"])
            perms = data.get("permissions")
            repos = [r["name"] if isinstance(r, dict) else str(r) for r in data.get("repositories", [])] or repositories
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise TokenBrokerError(f"Malformed response from token exchange: {exc}") from None

        new_tok = InstallationToken(int(inst_key), raw_tok, expires_at, permissions=perms, repositories=repos)
        self._cache[cache_key] = new_tok
        return new_tok

    def revoke_token(self, token: InstallationToken) -> None:
        self._cache.pop((str(token.installation_id), token.repositories), None)
