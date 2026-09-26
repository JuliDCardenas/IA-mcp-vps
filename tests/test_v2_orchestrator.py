from __future__ import annotations

import io
import json
import shutil
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from typing import Any
import unittest

from dari_mcp_vps.job_validator import SECRET_PATTERNS
from dari_mcp_vps.v2.identity import (
    InstallationNotAllowedError,
    InvalidRepositoryFormatError,
    OwnerNotAllowedError,
    RepositoryIdentity,
    parse_repository_identity,
    validate_repository_identity,
)
from dari_mcp_vps.v2.token_broker import (
    GitHubAppTokenBroker,
    InstallationToken,
    SecretToken,
    StaticInstallationTokenBroker,
    TokenBrokerAuthError,
    TokenBrokerConfigError,
    TokenBrokerError,
    redact_secrets,
)
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
from dari_mcp_vps.v2.policy import (
    RepositoryPolicyRegistry,
    RepositoryPolicyV2,
    build_conservative_policy,
)
from dari_mcp_vps.worktree_manager import (
    DEFAULT_REPOSITORY_POLICIES,
    RepositoryNotFoundError,
    RepositoryPolicy,
    WorktreeManager,
)


class MockHTTPResponse:
    def __init__(self, status: int = 200, body: Any = "", headers: dict[str, str] | None = None) -> None:
        self.status = self.code = status
        self._data = json.dumps(body).encode("utf-8") if isinstance(body, (dict, list)) else (body.encode("utf-8") if isinstance(body, str) else body)
        self.headers = headers or {}

    def read(self, amt: int | None = None) -> bytes:
        if amt is None:
            return self._data
        res, self._data = self._data[:amt], self._data[amt:]
        return res

    def __enter__(self) -> MockHTTPResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class TestRepositoryIdentity(unittest.TestCase):
    def test_valid_canonical_identity(self) -> None:
        repo = RepositoryIdentity.from_string("JuliDCardenas/IA-mcp-vps")
        self.assertEqual(repo.owner, "JuliDCardenas")
        self.assertEqual(repo.name, "IA-mcp-vps")
        self.assertEqual(repo.canonical, "JuliDCardenas/IA-mcp-vps")
        self.assertEqual(str(repo), "JuliDCardenas/IA-mcp-vps")
        self.assertIn("JuliDCardenas", repr(repo))

    def test_case_insensitive_equality_and_hashing(self) -> None:
        r1 = RepositoryIdentity("JuliDCardenas", "IA-mcp-vps")
        r2 = RepositoryIdentity("julidcardenas", "ia-mcp-vps")
        self.assertEqual(r1, r2)
        self.assertEqual(hash(r1), hash(r2))

    def test_whitespace_trimming(self) -> None:
        repo = RepositoryIdentity.from_string("  JuliDCardenas  /  IA-mcp-vps  ")
        self.assertEqual(repo.canonical, "JuliDCardenas/IA-mcp-vps")

    def test_rejection_of_invalid_formats(self) -> None:
        invalid = [
            "", "   ", "no-slash", "a/b/c", "/repo", "owner/", "owner//repo",
            "../etc/passwd", "owner/../passwd", "owner/repo\x00extra", "owner/repo.git",
            "-invalid-owner/repo", "owner-/repo", "owner--hyphens/repo",
            "owner/has space", "owner/repo;cmd", "owner/repo|pipe",
        ]
        for case in invalid:
            with self.subTest(case=case):
                with self.assertRaises(InvalidRepositoryFormatError):
                    RepositoryIdentity.from_string(case)

    def test_validation_against_allowed_owners(self) -> None:
        repo = RepositoryIdentity.from_string("JuliDCardenas/IA-mcp-vps")
        repo.validate_owner(["julidcardenas", "other-org"])
        with self.assertRaises(OwnerNotAllowedError):
            repo.validate_owner(["unauthorized-org"])
        with self.assertRaises(OwnerNotAllowedError):
            repo.validate_owner([])

    def test_validation_against_allowed_installations(self) -> None:
        repo = RepositoryIdentity.from_string("JuliDCardenas/IA-mcp-vps")
        repo.validate_installation(12345, [12345, "67890"])
        repo.validate_installation("67890", [12345, "67890"])
        with self.assertRaises(InstallationNotAllowedError):
            repo.validate_installation(99999, [12345, 67890])
        with self.assertRaises(InstallationNotAllowedError):
            repo.validate_installation(12345, [])

    def test_convenience_validation_helper(self) -> None:
        validated = validate_repository_identity(
            "JuliDCardenas/IA-mcp-vps",
            allowed_owners=["JuliDCardenas"],
            installation_id=12345,
            allowed_installations=[12345],
        )
        self.assertEqual(validated.canonical, "JuliDCardenas/IA-mcp-vps")

    def test_validation_boundaries_and_require_installation(self) -> None:
        ident = parse_repository_identity("org1/repo")
        with self.assertRaises(OwnerNotAllowedError):
            ident.validate_owner([])
        with self.assertRaises(InstallationNotAllowedError):
            ident.validate_installation(12345, [])
        with self.assertRaises(InstallationNotAllowedError):
            validate_repository_identity(ident, allowed_owners=["org1"], require_installation=True)
        ok = validate_repository_identity(
            ident,
            allowed_owners=["org1"],
            installation_id=12345,
            allowed_installations=[12345],
            require_installation=True,
        )
        self.assertEqual(ok.canonical, "org1/repo")


class TestInstallationTokenBroker(unittest.TestCase):
    def test_secret_token_redaction_boundary(self) -> None:
        sample = "mock-secret-val"
        sec = SecretToken(sample)
        self.assertEqual(str(sec), "[REDACTED]")
        self.assertEqual(repr(sec), "'[REDACTED]'")
        self.assertNotIn(sample, str(sec))
        self.assertEqual(sec.expose_secret(), sample)

    def test_installation_token_redaction_and_models(self) -> None:
        sample = "mock-token-val"
        inst = InstallationToken(
            123456,
            sample,
            datetime.now(timezone.utc) + timedelta(hours=1),
            permissions={"contents": "read"},
            repositories=["IA-mcp-vps"],
        )
        self.assertEqual(str(inst), "[REDACTED_INSTALLATION_TOKEN]")
        self.assertIn("[REDACTED]", repr(inst))
        self.assertNotIn(sample, str(inst))
        safe = inst.to_safe_dict()
        self.assertEqual(safe["token"], "[REDACTED]")
        self.assertNotIn(sample, json.dumps(safe))
        self.assertEqual(safe["installation_id"], 123456)

    def test_installation_token_expiration_logic(self) -> None:
        now = datetime.now(timezone.utc)
        tok_valid = InstallationToken(1, "mock-tok", now + timedelta(minutes=10))
        self.assertFalse(tok_valid.is_expired(buffer_seconds=60))
        tok_near = InstallationToken(1, "mock-tok", now + timedelta(seconds=30))
        self.assertTrue(tok_near.is_expired(buffer_seconds=60))
        tok_exp = InstallationToken(1, "mock-tok", now - timedelta(seconds=10))
        self.assertTrue(tok_exp.is_expired(buffer_seconds=0))

    def test_static_token_broker_caching_and_refresh(self) -> None:
        broker = StaticInstallationTokenBroker(default_ttl_seconds=3600)
        t1 = broker.get_installation_token(100)
        t2 = broker.get_installation_token(100)
        self.assertEqual(t1.expose_secret(), t2.expose_secret())
        self.assertEqual(broker.call_count, 2)
        broker.revoke_token(t1)
        t3 = broker.get_installation_token(100)
        self.assertNotEqual(t1.expose_secret(), t3.expose_secret())

    def test_static_broker_default_token_is_neutral(self) -> None:
        broker = StaticInstallationTokenBroker()
        raw = broker.get_installation_token(installation_id=12345).expose_secret()
        for pat in SECRET_PATTERNS:
            self.assertIsNone(pat.search(raw), f"Matched {pat.pattern}")

    def test_github_app_token_broker_fail_closed_without_config(self) -> None:
        broker = GitHubAppTokenBroker()
        self.assertFalse(broker.is_configured)
        with self.assertRaises(TokenBrokerConfigError):
            broker.get_installation_token(12345)

    def test_github_app_token_broker_mock_exchange(self) -> None:
        auth_val = "mock-exchange-token"
        def mock_transport(req: urllib.request.Request, timeout: int = 30) -> MockHTTPResponse:
            self.assertTrue(req.headers.get("Authorization", "").startswith("Bearer "))
            return MockHTTPResponse(201, {"token": auth_val, "expires_at": "2026-09-26T23:59:59Z"})
        broker = GitHubAppTokenBroker(jwt_signer=lambda app_id: "mock.jwt", http_transport=mock_transport)
        tok = broker.get_installation_token(55555)
        self.assertEqual(tok.installation_id, 55555)
        self.assertEqual(tok.expose_secret(), auth_val)

    def test_github_app_token_broker_error_redaction(self) -> None:
        leak = "leak-secret-data"
        def mock_fail(req: urllib.request.Request, timeout: int = 30) -> MockHTTPResponse:
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", None, io.BytesIO(f"Auth failed for {leak}".encode("utf-8")))
        broker = GitHubAppTokenBroker(jwt_signer=lambda app_id: "mock.jwt", http_transport=mock_fail)
        with self.assertRaises(TokenBrokerAuthError) as ctx:
            broker.get_installation_token(1)
        self.assertNotIn(leak, str(ctx.exception))
        self.assertIn("[REDACTED]", str(ctx.exception))


class TestRepositoryDiscovery(unittest.TestCase):
    def setUp(self) -> None:
        self.token_broker = StaticInstallationTokenBroker(fixed_token_value="mock-disc-token")

    def test_discovery_single_page(self) -> None:
        repos = [
            {"id": 101, "name": "IA-mcp-vps", "full_name": "JuliDCardenas/IA-mcp-vps", "owner": {"login": "JuliDCardenas"}, "default_branch": "main", "private": True},
            {"id": 102, "name": "repositorio-bd-emision", "full_name": "JuliDCardenas/repositorio-bd-emision", "owner": {"login": "JuliDCardenas"}, "default_branch": "main", "private": True},
        ]
        transport = lambda req, timeout=30: MockHTTPResponse(200, {"total_count": 2, "repositories": repos})
        catalog = GitHubRepositoryCatalog(self.token_broker, allowed_owners=["JuliDCardenas"], allowed_installations=[12345], http_transport=transport)
        page = catalog.list_repositories(12345)
        self.assertEqual(page.total_count, 2)
        self.assertEqual(len(page.repositories), 2)
        self.assertEqual(page.repositories[0].canonical, "JuliDCardenas/IA-mcp-vps")

    def test_discovery_pagination_handling(self) -> None:
        urls: list[str] = []
        def mock_paginated(req: urllib.request.Request, timeout: int = 30) -> MockHTTPResponse:
            urls.append(req.full_url)
            if "page=1" in req.full_url:
                return MockHTTPResponse(200, {"total_count": 3, "repositories": [{"id": 1, "name": "r1", "owner": {"login": "JuliDCardenas"}}, {"id": 2, "name": "r2", "owner": {"login": "JuliDCardenas"}}]}, headers={"Link": '<url?page=2>; rel="next"'})
            return MockHTTPResponse(200, {"total_count": 3, "repositories": [{"id": 3, "name": "r3", "owner": {"login": "JuliDCardenas"}}]})
        catalog = GitHubRepositoryCatalog(self.token_broker, allowed_owners=["JuliDCardenas"], allowed_installations=[12345], http_transport=mock_paginated)
        all_repos = catalog.list_all_repositories(12345, per_page=2)
        self.assertEqual([r.name for r in all_repos], ["r1", "r2", "r3"])
        self.assertEqual(len(urls), 2)

    def test_filtering_non_allowed_owners(self) -> None:
        repos = [
            {"id": 1, "name": "allowed-repo", "owner": {"login": "JuliDCardenas"}},
            {"id": 2, "name": "foreign-repo", "owner": {"login": "AttackerOrg"}},
        ]
        transport = lambda req, timeout=30: MockHTTPResponse(200, {"total_count": 2, "repositories": repos})
        catalog = GitHubRepositoryCatalog(self.token_broker, allowed_owners=["JuliDCardenas"], allowed_installations=[12345], http_transport=transport)
        page = catalog.list_repositories(12345)
        self.assertEqual(len(page.repositories), 1)
        self.assertEqual(page.repositories[0].name, "allowed-repo")

    def test_select_repository_success_and_not_found(self) -> None:
        repos = [{"id": 10, "name": "IA-mcp-vps", "owner": {"login": "JuliDCardenas"}}]
        transport = lambda req, timeout=30: MockHTTPResponse(200, {"total_count": 1, "repositories": repos})
        catalog = GitHubRepositoryCatalog(self.token_broker, allowed_owners=["JuliDCardenas"], allowed_installations=[12345], http_transport=transport)
        sel = catalog.select_repository("JuliDCardenas/IA-mcp-vps", 12345)
        self.assertEqual(sel.name, "IA-mcp-vps")
        with self.assertRaises(RepositoryNotFoundError):
            catalog.select_repository("JuliDCardenas/non-existent-repo", 12345)

    def test_discovery_error_redaction_and_handling(self) -> None:
        leak = "leak-auth-code"
        def mock_err(req: urllib.request.Request, timeout: int = 30) -> MockHTTPResponse:
            raise urllib.error.HTTPError(req.full_url, 401, "Bad credentials", None, io.BytesIO(f"Auth error: {leak}".encode("utf-8")))
        catalog = GitHubRepositoryCatalog(self.token_broker, allowed_owners=["JuliDCardenas"], allowed_installations=[12345], http_transport=mock_err)
        with self.assertRaises(DiscoveryAuthenticationError) as ctx:
            catalog.list_repositories(12345)
        self.assertNotIn(leak, str(ctx.exception))
        self.assertIn("[REDACTED]", str(ctx.exception))

    def test_clone_url_sanitization(self) -> None:
        auth_url = "https://user:opaque-mock@github.com/JuliDCardenas/test-repo.git"
        repo = DiscoveredRepository(id=1, owner="JuliDCardenas", name="test-repo", full_name="JuliDCardenas/test-repo", clone_url=auth_url)
        self.assertNotIn("opaque-mock", repo.clone_url)
        self.assertEqual(repo.clone_url, "https://github.com/JuliDCardenas/test-repo.git")

    def test_discovery_fail_closed_absent_boundaries(self) -> None:
        cat_no_inst = GitHubRepositoryCatalog(self.token_broker, allowed_owners=["JuliDCardenas"], allowed_installations=None)
        with self.assertRaises(InstallationNotAllowedError):
            cat_no_inst.list_repositories(12345)
        with self.assertRaises(InstallationNotAllowedError):
            cat_no_inst.select_repository("JuliDCardenas/IA-mcp-vps", 12345)

        cat_no_owners = GitHubRepositoryCatalog(self.token_broker, allowed_owners=None, allowed_installations=[12345])
        with self.assertRaises(OwnerNotAllowedError):
            cat_no_owners.list_repositories(12345)
        with self.assertRaises(OwnerNotAllowedError):
            cat_no_owners.select_repository("JuliDCardenas/IA-mcp-vps", 12345)

        cat_valid = GitHubRepositoryCatalog(self.token_broker, allowed_owners=["JuliDCardenas"], allowed_installations=[12345])
        for bad_id in (None, "", 99999):
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(InstallationNotAllowedError):
                    cat_valid.list_repositories(bad_id)
                with self.assertRaises(InstallationNotAllowedError):
                    cat_valid.select_repository("JuliDCardenas/IA-mcp-vps", bad_id)

        with self.assertRaises(OwnerNotAllowedError):
            cat_valid.select_repository("other-org/foreign-repo", 12345)


class TestRepositoryPolicy(unittest.TestCase):
    def test_conservative_default_for_unknown_repository(self) -> None:
        reg = RepositoryPolicyRegistry(allowed_owners=["JuliDCardenas"], allowed_installations=[12345])
        pol = reg.get_policy("JuliDCardenas/unknown-microservice", installation_id=12345)
        self.assertTrue(pol.is_conservative_default)
        self.assertFalse(pol.allow_promotion)
        self.assertFalse(pol.allow_tests)
        self.assertEqual(pol.test_commands, ())
        self.assertEqual(pol.installation_id, "12345")

    def test_unknown_repository_unauthorized_owner_rejected(self) -> None:
        reg = RepositoryPolicyRegistry(allowed_owners=["JuliDCardenas"], allowed_installations=[12345])
        with self.assertRaises(OwnerNotAllowedError):
            reg.get_policy("EvilOrg/malicious-repo", installation_id=12345)

    def test_configured_repository_policy(self) -> None:
        reg = RepositoryPolicyRegistry(allowed_owners=["JuliDCardenas"], allowed_installations=[12345])
        custom = RepositoryPolicyV2(
            identity=RepositoryIdentity("JuliDCardenas", "special-repo"),
            default_base_branch="develop",
            allow_promotion=True,
            allow_tests=True,
            test_commands=(("pytest", "tests"),),
            alias="special_repo",
            installation_id=12345,
        )
        reg.register_policy(custom)
        p1 = reg.get_policy("JuliDCardenas/special-repo", installation_id=12345)
        self.assertTrue(p1.allow_promotion)
        self.assertEqual(p1.default_base_branch, "develop")
        p2 = reg.get_policy("special_repo", installation_id=12345)
        self.assertEqual(p1, p2)

    def test_dynamic_reloading_without_code_changes(self) -> None:
        cfg = {
            "repositories": {
                "allowed_owners": ["JuliDCardenas"],
                "allowed_installations": [12345],
                "policies": {
                    "JuliDCardenas/dynamic-repo": {
                        "default_base_branch": "main",
                        "allow_promotion": False,
                        "allow_tests": False,
                        "installation_id": 12345,
                    }
                },
            }
        }
        reg = RepositoryPolicyRegistry(config_source=cfg)
        p1 = reg.get_policy("JuliDCardenas/dynamic-repo", installation_id=12345)
        self.assertFalse(p1.allow_promotion)
        cfg["repositories"]["policies"]["JuliDCardenas/dynamic-repo"]["allow_promotion"] = True
        cfg["repositories"]["policies"]["JuliDCardenas/dynamic-repo"]["test_commands"] = [["make", "test"]]
        reg.reload()
        p2 = reg.get_policy("JuliDCardenas/dynamic-repo", installation_id=12345)
        self.assertTrue(p2.allow_promotion)
        self.assertEqual(p2.test_commands, (("make", "test"),))

    def test_atomic_reload_removes_revoked_dynamic_access_immediately(self) -> None:
        cfg = {
            "repositories": {
                "allowed_owners": ["org-a"],
                "allowed_installations": [101],
                "policies": {"org-a/repo-a": {"default_base_branch": "main", "allow_promotion": True, "installation_id": 101}},
            }
        }
        reg = RepositoryPolicyRegistry(config_source=cfg)
        self.assertTrue(reg.get_policy("org-a/repo-a", installation_id=101).allow_promotion)
        cfg["repositories"]["policies"] = {"org-b/repo-b": {"default_base_branch": "main", "installation_id": 202}}
        cfg["repositories"]["allowed_owners"] = ["org-b"]
        cfg["repositories"]["allowed_installations"] = [202]
        reg.reload()
        self.assertEqual(reg.allowed_owners, {"org-b"})
        self.assertEqual(reg.allowed_installations, {"202"})
        with self.assertRaises(OwnerNotAllowedError):
            reg.get_policy("org-a/repo-a", installation_id=101)
        pol_b = reg.get_policy("org-b/repo-b", installation_id=202)
        self.assertEqual(pol_b.identity.canonical, "org-b/repo-b")
        v1_pol = reg.get_policy("ia_mcp_vps")
        self.assertEqual(v1_pol.alias, "ia_mcp_vps")

    def test_fail_closed_absent_boundaries_and_installation_id(self) -> None:
        reg_no_inst = RepositoryPolicyRegistry(allowed_owners=["JuliDCardenas"], allowed_installations=None)
        with self.assertRaises(InstallationNotAllowedError):
            reg_no_inst.get_policy("JuliDCardenas/some-repo", installation_id=12345)
        reg_no_owners = RepositoryPolicyRegistry(allowed_owners=None, allowed_installations=[12345])
        with self.assertRaises(OwnerNotAllowedError):
            reg_no_owners.get_policy("JuliDCardenas/some-repo", installation_id=12345)
        reg_valid = RepositoryPolicyRegistry(allowed_owners=["JuliDCardenas"], allowed_installations=[12345])
        for bad_id in (None, "", 99999):
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(InstallationNotAllowedError):
                    reg_valid.get_policy("JuliDCardenas/some-repo", installation_id=bad_id)

    def test_v1_adaptation_keeps_allow_promotion_false(self) -> None:
        reg = RepositoryPolicyRegistry(allowed_owners=["JuliDCardenas"], allowed_installations=[12345])
        v1_pol = reg.get_policy("ia_mcp_vps")
        self.assertTrue(v1_pol.allow_tests)
        self.assertFalse(v1_pol.allow_promotion)
        v1_priv = reg.get_policy("repositorio_bd_emision")
        self.assertFalse(v1_priv.allow_promotion)
        custom_v1 = {
            "custom": RepositoryPolicy(alias="custom", clone_url="https://github.com/org/c.git", github_repo="org/c", test_commands=(("pytest",),))
        }
        reg_custom = RepositoryPolicyRegistry(fallback_v1=custom_v1)
        self.assertFalse(reg_custom.get_policy("custom").allow_promotion)

    def test_v1_aliases_remain_compatible_without_installation_id(self) -> None:
        reg_empty = RepositoryPolicyRegistry(allowed_owners=None, allowed_installations=None)
        p_v1 = reg_empty.get_policy("ia_mcp_vps")
        self.assertEqual(p_v1.alias, "ia_mcp_vps")
        self.assertFalse(p_v1.allow_promotion)

    def test_duck_type_compatibility_with_dict(self) -> None:
        reg = RepositoryPolicyRegistry(allowed_owners=["JuliDCardenas"], allowed_installations=[12345])
        v1_pol = reg["ia_mcp_vps"]
        self.assertEqual(v1_pol.alias, "ia_mcp_vps")
        self.assertIn("ia_mcp_vps", reg)
        self.assertIn("repositorio_bd_emision", reg)
        self.assertIn("JuliDCardenas/IA-mcp-vps", reg)
        self.assertNotIn("unknown_attacker/repo", reg)


class TestV1Compatibility(unittest.TestCase):
    def test_default_repository_policies_preserved(self) -> None:
        self.assertIn("ia_mcp_vps", DEFAULT_REPOSITORY_POLICIES)
        self.assertIn("repositorio_bd_emision", DEFAULT_REPOSITORY_POLICIES)
        p1 = DEFAULT_REPOSITORY_POLICIES["ia_mcp_vps"]
        self.assertEqual(p1.alias, "ia_mcp_vps")
        self.assertEqual(p1.default_base_branch, "main")

    def test_worktree_manager_uses_default_policies_by_default(self) -> None:
        temp_dir = tempfile.mkdtemp()
        try:
            wm = WorktreeManager(storage_root=Path(temp_dir))
            policy = wm.get_policy("ia_mcp_vps")
            self.assertEqual(policy.alias, "ia_mcp_vps")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
