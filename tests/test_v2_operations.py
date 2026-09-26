from __future__ import annotations

import os
import json
import base64
import subprocess
import tempfile
import unittest
from pathlib import Path

from dari_mcp_vps.persistent_job import PersistentJobManager
from dari_mcp_vps.job_validator import ValidationError
from dari_mcp_vps.v2.mechanical import ConfinedMechanicalOperations
from dari_mcp_vps.v2.policy import RepositoryPolicyRegistry
from dari_mcp_vps.v2.token_broker import StaticInstallationTokenBroker
from dari_mcp_vps.v2.token_broker import GitHubAppTokenBroker
from dari_mcp_vps.v2.worktrees import GitHubNativeWorktreeManager
from dari_mcp_vps.worktree_manager import RepositoryPolicy, SecurityError, WorktreeManager


def git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


class TestGitHubNativeWorktrees(unittest.TestCase):
    def test_selection_is_dynamic_stable_and_contains_no_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RepositoryPolicyRegistry(
                allowed_owners=["JuliDCardenas"],
                allowed_installations=[77],
            )
            broker = StaticInstallationTokenBroker(fixed_token_value="sensitive-test-token")
            manager = GitHubNativeWorktreeManager(root, registry, broker, policies={})
            first = manager.select_repository("JuliDCardenas/new-repo", 77)
            second = manager.select_repository("JuliDCardenas/new-repo", 77)
            self.assertEqual(first, second)
            self.assertNotIn("/", first.runtime_alias)
            self.assertEqual(manager.get_policy(first.runtime_alias).github_repo, first.canonical)
            metadata = (root / "metadata" / f"repository_{first.runtime_alias}.json").read_text()
            self.assertNotIn("sensitive-test-token", metadata)

    def test_authenticated_git_keeps_token_out_of_arguments_and_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RepositoryPolicyRegistry(
                allowed_owners=["JuliDCardenas"],
                allowed_installations=[77],
            )
            broker = StaticInstallationTokenBroker(fixed_token_value="sensitive-test-token")
            manager = GitHubNativeWorktreeManager(root, registry, broker, policies={})
            selected = manager.select_repository("JuliDCardenas/new-repo", 77)
            with self.assertRaises(Exception) as ctx:
                manager._run_authenticated_git(["not-a-real-subcommand"], selected)
            self.assertNotIn("sensitive-test-token", str(ctx.exception))

    def test_live_jwt_signing_uses_short_lived_rs256_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "app.pem"
            subprocess.run(
                ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key)],
                capture_output=True,
                check=True,
            )
            broker = GitHubAppTokenBroker(app_id=12345, private_key=str(key))
            encoded = broker._generate_app_jwt().split(".")
            self.assertEqual(len(encoded), 3)
            payload = encoded[1] + "=" * (-len(encoded[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            self.assertEqual(claims["iss"], "12345")
            self.assertLessEqual(claims["exp"] - claims["iat"], 600)


class TestMechanicalOperations(unittest.TestCase):
    def test_delete_and_normalize_are_confined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "nested").mkdir()
            target = root / "nested" / "sample.txt"
            target.write_bytes(b"line\r\n\r\n")
            ops = ConfinedMechanicalOperations(root)
            normalized = ops.normalize_eof("nested/sample.txt")
            self.assertTrue(normalized.changed)
            self.assertEqual(target.read_bytes(), b"line\n")
            deleted = ops.delete_file("nested/sample.txt")
            self.assertTrue(deleted.changed)
            self.assertFalse(target.exists())
            with self.assertRaises(SecurityError):
                ops.delete_file("../outside.txt")

    def test_formatter_must_be_allowlisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ops = ConfinedMechanicalOperations(Path(tmp))
            with self.assertRaises(SecurityError):
                ops.format("arbitrary")


class TestValidateOnly(unittest.TestCase):
    def test_validate_only_and_mechanical_fix_do_not_invoke_agy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin = root / "origin"
            origin.mkdir()
            git(origin, "init", "-b", "main")
            git(origin, "config", "user.name", "Test")
            git(origin, "config", "user.email", "test@example.invalid")
            (origin / "README.md").write_text("base\n", encoding="utf-8")
            git(origin, "add", "README.md")
            git(origin, "commit", "-m", "base")

            storage = root / "storage"
            policy = RepositoryPolicy(
                alias="test_repo",
                clone_url=str(origin),
                github_repo="example/test",
            )
            worktrees = WorktreeManager(storage, policies={"test_repo": policy})

            class NeverAgy:
                is_isolated_environment_configured = True

                def execute(self, **kwargs):
                    raise AssertionError("Agy must not be invoked")

            manager = PersistentJobManager(
                storage,
                worktree_manager=worktrees,
                execution_adapter=NeverAgy(),
            )
            job = manager.create_job(
                repository="test_repo",
                goal="Change README",
                acceptance_criteria=["README changed"],
                work_item_id="validate_only",
                task_type="implement",
            )
            readme = Path(job.worktree_path) / "README.md"
            readme.write_bytes(b"changed\r\n\r\n")
            with self.assertRaises(ValidationError):
                manager.validate_only(job.job_id)
            self.assertEqual(manager.get_job(job.job_id).status, "FAILED")
            revision_count = manager.get_job(job.job_id).revision_count
            result, report2 = manager.apply_mechanical_operation(
                job.job_id,
                operation="normalize_eof",
                path="README.md",
            )
            self.assertTrue(result.changed)
            self.assertTrue(report2 and report2.passed)
            self.assertEqual(manager.get_job(job.job_id).revision_count, revision_count)
            self.assertEqual(readme.read_bytes(), b"changed\n")


if __name__ == "__main__":
    unittest.main()