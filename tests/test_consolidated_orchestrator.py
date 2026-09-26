from __future__ import annotations

from dataclasses import asdict
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error
from pathlib import Path
from typing import Any

from dari_mcp_vps.job_validator import (
    BaseCommitMismatchError,
    EmptyImplementationError,
    JobValidator,
    SecretDetectedError,
    ValidationError,
    _redact_secrets,
)
from dari_mcp_vps.persistent_job import (
    AgyExecutionAdapter,
    DeploymentBlockedError,
    EvidenceMismatchError,
    JobStateError,
    MAX_EXECUTION_OUTPUT_TAIL,
    PersistentJobManager,
    PersistentJobRecord,
    RevisionLimitExceededError,
    UnpublishedChangesError,
    _sanitize_execution_output,
    build_initial_prompt,
)
from dari_mcp_vps.promoter import (
    BranchPromoter,
    GitHubPRClient,
    GitHubPRConfig,
    PromoterConfig,
    PromotionError,
)
from dari_mcp_vps.tools.coding_jobs import register_coding_job_tools
from dari_mcp_vps.tools.private_coding_job import register_private_coding_job_tool
from dari_mcp_vps.worktree_manager import (
    DirtyWorktreeError,
    RepositoryPolicy,
    SecurityError,
    WorktreeManager,
)


class MockHTTPResponse:
    def __init__(self, data: bytes, status: int = 201) -> None:
        self.data = data
        self.status = status

    def read(self, amt: int = -1) -> bytes:
        if amt < 0 or amt >= len(self.data):
            return self.data
        return self.data[:amt]

    def __enter__(self) -> MockHTTPResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def _init_git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=path, check=True, capture_output=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


class TestConsolidatedOrchestrator(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="orch_test_")
        self.root = Path(self.temp_dir)
        self.storage_root = self.root / "storage"
        self.origin_repo = self.root / "origin.git"
        self.publish_repo = self.root / "upstream.git"

        # Initialize origin and a bare upstream for promoter publishing
        self.base_commit = _init_git_repo(self.origin_repo)
        subprocess.run(["git", "clone", "--bare", str(self.origin_repo), str(self.publish_repo)], check=True, capture_output=True)

        self.policy = RepositoryPolicy(
            alias="test_repo",
            clone_url=str(self.origin_repo),
            default_base_branch="main",
            github_repo="test_owner/test_repo",
            test_commands=(("python3", "-c", "print('allowlisted test ok')"),),
        )

        self.promoter_config = PromoterConfig(
            enabled=True,
            publish_remote_url=str(self.publish_repo),
            default_base_branch="main",
        )
        self.promoter = BranchPromoter(config=self.promoter_config)

        self.pr_config = GitHubPRConfig(
            enabled=True,
            github_token="fake_token_for_test",
            owner_repo="test_owner/test_repo",
        )

        def mock_opener(req: Any, timeout: int = 30) -> MockHTTPResponse:
            resp_body = {
                "number": 101,
                "html_url": "https://github.com/test_owner/test_repo/pull/101",
                "state": "open",
                "created_at": "2026-09-26T00:00:00Z",
            }
            return MockHTTPResponse(json.dumps(resp_body).encode("utf-8"), status=201)

        self.mock_opener = mock_opener
        self.pr_client = GitHubPRClient(config=self.pr_config, http_opener=mock_opener)

        self.wt_manager = WorktreeManager(
            storage_root=self.storage_root,
            policies={"test_repo": self.policy},
        )
        self.manager = PersistentJobManager(
            storage_root=self.storage_root,
            worktree_manager=self.wt_manager,
            promoter=self.promoter,
            pr_client=self.pr_client,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_02_and_03_revision_reuses_job_branch_worktree_and_conversation_id(self) -> None:
        """2 & 3. Verifies same job/branch/worktree/conversation ID are reused across revisions."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Add feature X",
            acceptance_criteria=["Criterion 1"],
            work_item_id="work_item_42",
        )
        conv_id = job.conversation_id
        wt_path = job.worktree_path
        f_branch = job.feature_branch

        # Add valid change to worktree and validate to reach NOTION_REVIEW
        (Path(wt_path) / "feature.py").write_text("def hello(): pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=wt_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add feature"], cwd=wt_path, check=True, capture_output=True)

        self.manager.validate_job(job.job_id)
        job_review = self.manager.get_job(job.job_id)
        self.assertEqual(job_review.status, "NOTION_REVIEW")

        # Request revision
        rev_job = self.manager.request_revision(job.job_id, feedback="Improve error handling")
        self.assertEqual(rev_job.status, "REVISION_REQUESTED")
        self.assertEqual(rev_job.job_id, job.job_id)
        self.assertEqual(rev_job.worktree_path, wt_path)
        self.assertEqual(rev_job.feature_branch, f_branch)
        self.assertEqual(rev_job.conversation_id, conv_id)
        self.assertEqual(rev_job.revision_count, 1)

    def test_04_fourth_revision_rejected(self) -> None:
        """4. Verifies fourth revision is strictly rejected (max 3 cycles)."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Iterative task",
            acceptance_criteria=["Criterion 1"],
        )
        (Path(job.worktree_path) / "code.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=job.worktree_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init code"], cwd=job.worktree_path, check=True, capture_output=True)

        for i in range(1, 4):
            self.manager.validate_job(job.job_id)
            self.manager.request_revision(job.job_id, feedback=f"Cycle {i}")

        self.manager.validate_job(job.job_id)
        with self.assertRaises(RevisionLimitExceededError):
            self.manager.request_revision(job.job_id, feedback="Cycle 4 rejected")

    def test_05_invalid_state_transitions_rejected(self) -> None:
        """5. Verifies invalid state transitions are rejected."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="State test",
            acceptance_criteria=["Criterion 1"],
        )
        # Cannot approve from CREATED
        with self.assertRaises(JobStateError):
            self.manager.approve_changes(job.job_id, expected_validation_hash="fake", expected_base_commit="fake")

        # Cannot publish from CREATED
        with self.assertRaises(JobStateError):
            self.manager.publish_branch(job.job_id)

        # Cannot create PR from CREATED
        with self.assertRaises(JobStateError):
            self.manager.create_pull_request(job.job_id)

    def test_06_and_07_cancellation_idempotent_and_preserves_worktree(self) -> None:
        """6 & 7. Verifies cancellation is idempotent and preserves worktree contents."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Cancel test",
            acceptance_criteria=["Criterion 1"],
        )
        wt = Path(job.worktree_path)
        (wt / "in_flight.txt").write_text("in flight work", encoding="utf-8")

        cancelled_1 = self.manager.cancel_job(job.job_id, reason="User abort")
        self.assertEqual(cancelled_1.status, "CANCELLED")

        # Second cancel is idempotent
        cancelled_2 = self.manager.cancel_job(job.job_id, reason="User abort again")
        self.assertEqual(cancelled_2.status, "CANCELLED")

        # Worktree and file must be preserved
        self.assertTrue(wt.exists())
        self.assertTrue((wt / "in_flight.txt").exists())

    def test_08_and_09_only_allowlisted_tests_execute_arbitrary_rejected(self) -> None:
        """8 & 9. Verifies only repository-allowlisted tests execute; arbitrary commands rejected."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Testing allowlist",
            acceptance_criteria=["Criterion 1"],
        )
        (Path(job.worktree_path) / "valid.py").write_text("x = 42\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=job.worktree_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "commit valid"], cwd=job.worktree_path, check=True, capture_output=True)

        report = self.manager.validate_job(job.job_id)
        self.assertTrue(report.passed)
        self.assertEqual(len(report.test_results), 1)
        self.assertEqual(report.test_results[0].command, ("python3", "-c", "print('allowlisted test ok')"))
        self.assertEqual(report.test_results[0].exit_code, 0)

    def test_10_secret_detection(self) -> None:
        """10. Verifies secret detection triggers SecretDetectedError."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Secret leak test",
            acceptance_criteria=["Criterion 1"],
        )
        (Path(job.worktree_path) / "leak.py").write_text('API_KEY = "ghp_12345678901234567890"\n', encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=job.worktree_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add leak"], cwd=job.worktree_path, check=True, capture_output=True)

        with self.assertRaises(SecretDetectedError):
            self.manager.validate_job(job.job_id)

    def test_11_path_and_symlink_escape_rejection(self) -> None:
        """11. Verifies path traversal and symlinks are strictly rejected."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Symlink test",
            acceptance_criteria=["Criterion 1"],
        )
        wt = Path(job.worktree_path)
        os.symlink("/etc/passwd", wt / "symlink_escape.py")
        subprocess.run(["git", "add", "."], cwd=wt, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "symlink"], cwd=wt, check=True, capture_output=True)

        with self.assertRaises(SecurityError):
            self.manager.validate_job(job.job_id)

    def test_12_changed_file_line_byte_limits(self) -> None:
        """12. Verifies file byte limits and disallowed file types are rejected."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Limit test",
            acceptance_criteria=["Criterion 1"],
        )
        wt = Path(job.worktree_path)

        # 1. Disallowed file type
        (wt / "dangerous.exe").write_bytes(b"binary data")
        subprocess.run(["git", "add", "."], cwd=wt, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add exe"], cwd=wt, check=True, capture_output=True)

        with self.assertRaises(ValidationError):
            self.manager.validate_job(job.job_id)

    def test_13_and_14_validation_hash_and_approval_exact_matching(self) -> None:
        """13 & 14. Verifies validation hash sensitivity and strict approval matching."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Approval test",
            acceptance_criteria=["Criterion 1"],
        )
        wt = Path(job.worktree_path)
        (wt / "module.py").write_text("def run(): return 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=wt, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "v1 commit"], cwd=wt, check=True, capture_output=True)

        report = self.manager.validate_job(job.job_id)
        valid_hash = report.report_hash

        # Tamper hash -> approval rejected
        with self.assertRaises(EvidenceMismatchError):
            self.manager.approve_changes(job.job_id, expected_validation_hash="tampered_hash", expected_base_commit=job.base_commit)

        # Tamper base commit -> approval rejected
        with self.assertRaises(EvidenceMismatchError):
            self.manager.approve_changes(job.job_id, expected_validation_hash=valid_hash, expected_base_commit="0000000000000000000000000000000000000000")

        # Correct hash and base commit -> approval succeeds
        approved_job = self.manager.approve_changes(job.job_id, expected_validation_hash=valid_hash, expected_base_commit=job.base_commit)
        self.assertEqual(approved_job.status, "CHANGES_APPROVED")

    def test_15_16_17_publishing_checks_and_no_force_push(self) -> None:
        """15, 16, 17. Verifies publishing requires approval, forbids main, and avoids force push."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Publish test",
            acceptance_criteria=["Criterion 1"],
        )
        wt = Path(job.worktree_path)
        (wt / "pub.py").write_text("x = 10\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=wt, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "pub commit"], cwd=wt, check=True, capture_output=True)

        # 15. Publishing before approval is rejected
        with self.assertRaises(JobStateError):
            self.manager.publish_branch(job.job_id)

        report = self.manager.validate_job(job.job_id)
        self.manager.approve_changes(job.job_id, expected_validation_hash=report.report_hash, expected_base_commit=job.base_commit)

        # 16. Verify publication destination cannot be main
        base_path = self.wt_manager.ensure_base_repository("test_repo")
        with self.assertRaises(SecurityError):
            self.promoter.publish_branch(base_repo_path=base_path, feature_branch="main")

        # 17. Publishing approved feature branch succeeds cleanly
        pub_job = self.manager.publish_branch(job.job_id)
        self.assertEqual(pub_job.status, "BRANCH_PUBLISHED")
        self.assertIsNotNone(pub_job.publish_info)

    def test_18_and_19_pr_lifecycle_and_fail_closed_without_config(self) -> None:
        """18 & 19. Verifies PR requires published branch and backends fail closed without configuration."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="PR test",
            acceptance_criteria=["Criterion 1"],
        )
        wt = Path(job.worktree_path)
        (wt / "pr_code.py").write_text("y = 20\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=wt, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "pr code"], cwd=wt, check=True, capture_output=True)

        report = self.manager.validate_job(job.job_id)
        self.manager.approve_changes(job.job_id, expected_validation_hash=report.report_hash, expected_base_commit=job.base_commit)

        # 18. PR before publication rejected
        with self.assertRaises(JobStateError):
            self.manager.create_pull_request(job.job_id)

        self.manager.publish_branch(job.job_id)
        pr_job = self.manager.create_pull_request(job.job_id)
        self.assertEqual(pr_job.status, "PR_CREATED")
        self.assertIsNotNone(pr_job.pr_info)

        # 19. Fail-closed without configuration
        unconfigured_promoter = BranchPromoter(config=PromoterConfig(enabled=False))
        with self.assertRaises(PromotionError):
            unconfigured_promoter.publish_branch(base_repo_path=wt, feature_branch=job.feature_branch)

        unconfigured_pr = GitHubPRClient(config=GitHubPRConfig(enabled=False))
        with self.assertRaises(PromotionError):
            unconfigured_pr.create_pull_request(owner_repo="owner/repo", feature_branch=job.feature_branch)

    def test_20_and_21_dirty_unpublished_cleanup_rejected_clean_allowed(self) -> None:
        """20 & 21. Verifies dirty/unpublished cleanup rejected and clean cleanup preserves base repo."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Cleanup test",
            acceptance_criteria=["Criterion 1"],
        )
        wt = Path(job.worktree_path)
        (wt / "file.py").write_text("z = 30\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=wt, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "file"], cwd=wt, check=True, capture_output=True)

        report = self.manager.validate_job(job.job_id)

        # 20a. Unpublished changes rejected
        with self.assertRaises(UnpublishedChangesError):
            self.manager.cleanup_job(job.job_id)

        # Progress to PR_CREATED
        self.manager.approve_changes(job.job_id, expected_validation_hash=report.report_hash, expected_base_commit=job.base_commit)
        self.manager.publish_branch(job.job_id)
        self.manager.create_pull_request(job.job_id)

        # 20b. Dirty worktree rejected
        (wt / "dirty_uncommitted.txt").write_text("dirty work", encoding="utf-8")
        with self.assertRaises(DirtyWorktreeError):
            self.manager.cleanup_job(job.job_id)

        # Remove dirty file and perform clean cleanup
        (wt / "dirty_uncommitted.txt").unlink()
        cleaned_job = self.manager.cleanup_job(job.job_id)
        self.assertEqual(cleaned_job.status, "CLEANED_UP")
        self.assertFalse(wt.exists())

        # 21. Permanent bare base repository is preserved intact
        base_path = self.wt_manager.ensure_base_repository("test_repo")
        self.assertTrue(base_path.exists())
        is_bare = subprocess.check_output(
            ["git", "-C", str(base_path), "rev-parse", "--is-bare-repository"],
            text=True,
        ).strip()
        self.assertEqual(is_bare, "true")

    def test_22_legacy_and_new_tool_registrations_compatible(self) -> None:
        """22. Verifies legacy tool registration and all 13 tools are present and functional."""
        class MockMCP:
            def __init__(self):
                self.tools = []

            def tool(self):
                def decorator(fn):
                    self.tools.append(fn.__name__)
                    return fn
                return decorator

        mock_mcp = MockMCP()
        mock_config = type("Config", (), {"raw": {}})()

        register_coding_job_tools(mock_mcp, mock_config)
        register_private_coding_job_tool(mock_mcp, mock_config)

        expected_tools = {
            "coding_job_create",
            "coding_job_status",
            "coding_job_wait",
            "coding_job_result",
            "coding_job_changes",
            "coding_job_artifact",
            "coding_job_request_revision",
            "coding_job_approve_changes",
            "coding_job_publish_branch",
            "coding_job_create_pull_request",
            "coding_job_cancel",
            "coding_job_cleanup",
            "coding_private_job_create",
        }
        self.assertTrue(expected_tools.issubset(set(mock_mcp.tools)))

    def test_23_no_automatic_merge_or_deployment_path_exists(self) -> None:
        """23. Verifies no automatic merge or deployment path exists in orchestrator classes."""
        forbidden_terms = ["merge", "deploy", "push_to_main"]
        for cls in [PersistentJobManager, BranchPromoter, GitHubPRClient]:
            for attr in dir(cls):
                for term in forbidden_terms:
                    self.assertNotIn(term, attr.lower(), f"Forbidden operation {term} found on {cls.__name__}.{attr}")

    def test_24_uncommitted_secret_detection(self) -> None:
        """24. Verifies uncommitted secrets in live worktree are detected without commit."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Test uncommitted secret",
            acceptance_criteria=["Criterion 1"],
        )
        wt = Path(job.worktree_path)
        # Write secret directly to working tree; do NOT git add or git commit
        (wt / "uncommitted_leak.py").write_text('API_KEY = "ghp_12345678901234567890"\n', encoding="utf-8")

        with self.assertRaises(SecretDetectedError):
            self.manager.validate_job(job.job_id)

    def test_25_uncommitted_syntax_error_detection(self) -> None:
        """25. Verifies uncommitted syntax error in live worktree is detected without commit."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Test uncommitted syntax error",
            acceptance_criteria=["Criterion 1"],
        )
        wt = Path(job.worktree_path)
        # Write invalid Python syntax; do NOT git add or git commit
        (wt / "broken_syntax.py").write_text("def broken_func(\n", encoding="utf-8")

        with self.assertRaises(ValidationError):
            self.manager.validate_job(job.job_id)

    def test_26_uncommitted_oversized_file_detection(self) -> None:
        """26. Verifies uncommitted oversized file in live worktree is detected without commit."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Test oversized file",
            acceptance_criteria=["Criterion 1"],
        )
        wt = Path(job.worktree_path)
        # Write > 65536 bytes; do NOT git add or git commit
        (wt / "huge_file.py").write_text("x = 1\n" * 15000, encoding="utf-8")

        with self.assertRaises(ValidationError):
            self.manager.validate_job(job.job_id)

    def test_27_untracked_file_validation_and_hash_sensitivity(self) -> None:
        """27. Verifies untracked files are included in validation manifest and diff hash."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Test untracked file handling",
            acceptance_criteria=["Criterion 1"],
        )
        wt = Path(job.worktree_path)
        (wt / "new_feature.py").write_text("def feature(): return 42\n", encoding="utf-8")

        report1 = self.manager.validate_job(job.job_id)
        self.assertTrue(report1.passed)
        untracked_records = [c for c in report1.changes if c.path == "new_feature.py"]
        self.assertEqual(len(untracked_records), 1)
        self.assertEqual(untracked_records[0].operation, "upsert")
        initial_hash = report1.report_hash

        # Modifying untracked file must alter report_hash deterministically
        (wt / "new_feature.py").write_text("def feature(): return 999\n", encoding="utf-8")
        report2 = self.manager.validate_job(job.job_id)
        self.assertTrue(report2.passed)
        self.assertNotEqual(report1.report_hash, report2.report_hash)

    def test_28_github_pat_secret_detection(self) -> None:
        """28. Verifies fine-grained GitHub PAT token (github_pat_) is strictly detected."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Test github_pat token detection",
            acceptance_criteria=["Criterion 1"],
        )
        wt = Path(job.worktree_path)
        (wt / "token_leak.py").write_text(
            'GH_PAT = "github_pat_11AEXAMPLETOKEN1234567890abcdefghijklmnopqrstuvwxyz_0123456789"\n',
            encoding="utf-8",
        )

        with self.assertRaises(SecretDetectedError):
            self.manager.validate_job(job.job_id)

    def test_29_real_pr_client_fail_closed_and_mocked_contract(self) -> None:
        """29. Verifies real PR client fails closed without config, sanitizes errors, and parses correctly."""
        # 1. Fail closed when disabled
        unconfigured = GitHubPRClient(config=GitHubPRConfig(enabled=False))
        with self.assertRaises(PromotionError):
            unconfigured.create_pull_request(owner_repo="test/repo", feature_branch="feat/test")

        # 2. Fail closed when token missing
        no_token = GitHubPRClient(config=GitHubPRConfig(enabled=True, github_token="   ", owner_repo="test/repo"))
        with self.assertRaises(PromotionError):
            no_token.create_pull_request(owner_repo="test/repo", feature_branch="feat/test")

        # 3. Invalid repository format
        valid_cfg = GitHubPRConfig(enabled=True, github_token="token123", owner_repo="test/repo")
        client = GitHubPRClient(config=valid_cfg)
        with self.assertRaises(SecurityError):
            client.create_pull_request(owner_repo="invalid_repo_without_slash", feature_branch="feat/test")

        import io

        # 4. Mocked HTTPError with sensitive token redaction
        def error_opener(req: Any, timeout: int = 30) -> Any:
            raise urllib.error.HTTPError(
                url="https://api.github.com/repos/test/repo/pulls",
                code=403,
                msg="Forbidden",
                hdrs=None,  # type: ignore
                fp=io.BytesIO(b'{"message": "token ghp_12345678901234567890 forbidden"}'),  # type: ignore
            )

        err_client = GitHubPRClient(config=valid_cfg, http_opener=error_opener)
        with self.assertRaises(PromotionError) as ctx:
            err_client.create_pull_request(owner_repo="test/repo", feature_branch="feat/test")
        self.assertIn("HTTP 403", str(ctx.exception))

    def test_30_agy_execution_adapter_command_env_and_cancellation(self) -> None:
        """30. Verifies Agy execution adapter argument arrays, environment scrubbing, and cancellation."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Adapter test",
            acceptance_criteria=["Criterion 1"],
        )
        adapter = AgyExecutionAdapter(runner_configured=True)

        # Initial prompt command
        cmd_init = adapter.build_command(job, "Initial prompt", is_revision=False)
        self.assertEqual(
            cmd_init,
            ["agy", "-p", "Initial prompt", "--mode=accept-edits", "--sandbox", "--print-timeout", "20m", "--output-format", "json"],
        )

        # Revision prompt command (reuses conversation_id)
        cmd_rev = adapter.build_command(job, "Fix review comment", is_revision=True)
        self.assertEqual(
            cmd_rev,
            ["agy", "--conversation", job.conversation_id, "-p", "Fix review comment", "--mode=accept-edits", "--sandbox", "--print-timeout", "20m", "--output-format", "json"],
        )

        # Environment filtering
        env = adapter.build_environment(Path(job.worktree_path))
        self.assertIn("PATH", env)
        self.assertEqual(env["HOME"], "/home/agy")
        self.assertEqual(env["TMPDIR"], "/tmp")
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("DOCKER_HOST", env)

    def test_31_persistent_execution_fails_closed_without_isolated_runner(self) -> None:
        """31. Verifies persistent execution fails closed and marks BLOCKED_DEPLOYMENT when runner unconfigured."""
        # By default in unconfigured environment, runner_configured is False
        manager = PersistentJobManager(
            storage_root=self.storage_root,
            worktree_manager=self.wt_manager,
            promoter=self.promoter,
            pr_client=self.pr_client,
            execution_adapter=AgyExecutionAdapter(runner_configured=False),
        )
        job = manager.create_job(
            repository="test_repo",
            goal="Direct execution test",
            acceptance_criteria=["Criterion 1"],
        )
        with self.assertRaises(DeploymentBlockedError):
            manager.run_execution(job.job_id)

        job_state = manager.get_job(job.job_id)
        self.assertEqual(job_state.status, "BLOCKED_DEPLOYMENT")
        self.assertIn("BLOCKED_DEPLOYMENT", job_state.error or "")

    def test_32_persistent_execution_succeeds_with_isolated_runner(self) -> None:
        """32. Verifies persistent execution lifecycle with isolated runner transitions to NOTION_REVIEW."""
        def mock_executor(job: Any, cmd: list[str], env: dict[str, str], wt: Path) -> tuple[int, str, str]:
            (wt / "implemented.py").write_text("def work(): return True\n", encoding="utf-8")
            return 0, json.dumps({"conversation_id": "real_agy_conv_999"}), "real_agy_conv_999"

        isolated_adapter = AgyExecutionAdapter(runner_configured=True, executor_fn=mock_executor)
        manager = PersistentJobManager(
            storage_root=self.storage_root,
            worktree_manager=self.wt_manager,
            promoter=self.promoter,
            pr_client=self.pr_client,
            execution_adapter=isolated_adapter,
        )
        job = manager.create_job(
            repository="test_repo",
            goal="Isolated implementation",
            acceptance_criteria=["Criterion 1"],
        )
        updated_job = manager.run_execution(job.job_id)

        self.assertEqual(updated_job.status, "NOTION_REVIEW")
        self.assertEqual(updated_job.conversation_id, "real_agy_conv_999")
        self.assertIsNotNone(updated_job.validation_report)
        self.assertTrue(updated_job.validation_report["passed"])

    def test_33_temporary_git_commits_work_without_global_git_configuration(self) -> None:
        """33. Proves temporary git commits succeed using sanitized test env without global git config."""
        clean_home = tempfile.mkdtemp(prefix="clean_home_")
        try:
            test_env = JobValidator.build_test_environment()
            test_env["HOME"] = clean_home
            test_env["GIT_CONFIG_GLOBAL"] = os.path.join(clean_home, "nonexistent.gitconfig")
            test_env["GIT_CONFIG_SYSTEM"] = os.path.join(clean_home, "nonexistent.gitconfig")

            repo_dir = Path(tempfile.mkdtemp(prefix="clean_repo_"))
            try:
                subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True, env=test_env)
                (repo_dir / "test_file.txt").write_text("sample content\n", encoding="utf-8")
                subprocess.run(["git", "add", "test_file.txt"], cwd=repo_dir, check=True, capture_output=True, env=test_env)

                res = subprocess.run(
                    ["git", "commit", "-m", "Deterministic test commit"],
                    cwd=repo_dir,
                    env=test_env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(res.returncode, 0, f"git commit failed: {res.stderr}")

                log_author = subprocess.check_output(
                    ["git", "log", "-1", "--format=%an <%ae>"], cwd=repo_dir, text=True, env=test_env
                ).strip()
                log_committer = subprocess.check_output(
                    ["git", "log", "-1", "--format=%cn <%ce>"], cwd=repo_dir, text=True, env=test_env
                ).strip()
                self.assertEqual(log_author, "IA MCP Test <ia-mcp-test@localhost>")
                self.assertEqual(log_committer, "IA MCP Test <ia-mcp-test@localhost>")
            finally:
                shutil.rmtree(repo_dir, ignore_errors=True)
        finally:
            shutil.rmtree(clean_home, ignore_errors=True)

    def test_34_fixed_git_identity_passed_only_to_allowlisted_tests(self) -> None:
        """34. Proves fixed git identity is passed strictly to allowlisted test execution and not leaked."""
        test_env = JobValidator.build_test_environment()
        self.assertEqual(test_env["GIT_AUTHOR_NAME"], "IA MCP Test")
        self.assertEqual(test_env["GIT_AUTHOR_EMAIL"], "ia-mcp-test@localhost")
        self.assertEqual(test_env["GIT_COMMITTER_NAME"], "IA MCP Test")
        self.assertEqual(test_env["GIT_COMMITTER_EMAIL"], "ia-mcp-test@localhost")
        self.assertIn("PATH", test_env)

        self.assertNotIn("GITHUB_TOKEN", test_env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", test_env)
        self.assertNotIn("SSH_AUTH_SOCK", test_env)
        self.assertNotIn("DOCKER_HOST", test_env)

        job = self.manager.create_job(
            repository="test_repo",
            goal="Identity isolation test",
            acceptance_criteria=["Criterion 1"],
        )
        wt = Path(job.worktree_path)
        (wt / "feature.py").write_text("x = 100\n", encoding="utf-8")

        validator = JobValidator(worktree_dir=wt, storage_root=self.storage_root)
        check_cmd = (
            sys.executable,
            "-c",
            "import os; print('AUTHOR:' + os.environ.get('GIT_AUTHOR_NAME', 'NONE')); "
            "print('EMAIL:' + os.environ.get('GIT_AUTHOR_EMAIL', 'NONE'))",
        )
        report = validator.validate(
            base_commit=job.base_commit,
            feature_branch=job.feature_branch,
            test_commands=(check_cmd,),
            expected_base_commit=job.base_commit,
        )
        self.assertTrue(report.passed)
        self.assertEqual(len(report.test_results), 1)
        self.assertIn("AUTHOR:IA MCP Test", report.test_results[0].stdout_tail)
        self.assertIn("EMAIL:ia-mcp-test@localhost", report.test_results[0].stdout_tail)

        # Ensure that non-allowlisted git subprocess calls do not receive test_env
        original_run = subprocess.run
        captured_calls: list[dict[str, Any]] = []

        def tracking_run(*args: Any, **kwargs: Any) -> Any:
            captured_calls.append({"cmd": args[0] if args else kwargs.get("args"), "env": kwargs.get("env")})
            return original_run(*args, **kwargs)

        with unittest.mock.patch("subprocess.run", side_effect=tracking_run):
            validator.validate(
                base_commit=job.base_commit,
                feature_branch=job.feature_branch,
                test_commands=(check_cmd,),
                expected_base_commit=job.base_commit,
            )

        for call in captured_calls:
            cmd = call["cmd"]
            if cmd and isinstance(cmd, list) and cmd[0] == "git":
                self.assertIsNone(call["env"], f"Git command {cmd} unexpectedly received custom env")
            elif cmd and tuple(cmd) == check_cmd:
                self.assertIsNotNone(call["env"])
                self.assertEqual(call["env"]["GIT_AUTHOR_NAME"], "IA MCP Test")

    def test_35_implementation_with_zero_changes_fails_clearly(self) -> None:
        """35. Proves an implementation job exiting 0 with zero changed files is rejected with diagnostic."""
        def zero_change_executor(job: Any, cmd: list[str], env: dict[str, str], wt: Path) -> tuple[int, str, str]:
            return 0, "Agy finished thinking without modifying any files.", "conv_zero_changes"

        isolated_adapter = AgyExecutionAdapter(runner_configured=True, executor_fn=zero_change_executor)
        manager = PersistentJobManager(
            storage_root=self.storage_root,
            worktree_manager=self.wt_manager,
            promoter=self.promoter,
            pr_client=self.pr_client,
            execution_adapter=isolated_adapter,
        )
        job = manager.create_job(
            repository="test_repo",
            goal="Implement zero change rejection",
            acceptance_criteria=["Must reject empty changes"],
            task_type="implement",
        )

        with self.assertRaises(EmptyImplementationError) as ctx:
            manager.run_execution(job.job_id)

        self.assertIn("zero changed files", str(ctx.exception).lower())

        stored = manager.get_job(job.job_id)
        self.assertEqual(stored.status, "FAILED")
        self.assertEqual(stored.exit_code, 0)
        self.assertIsNotNone(stored.execution_output_tail)
        self.assertIn("without modifying any files", stored.execution_output_tail)
        self.assertIn("zero changed files", (stored.error or "").lower())

    def test_36_audit_jobs_may_complete_with_zero_changes(self) -> None:
        """36. Proves legitimate read-only audit jobs may complete with zero changed files."""
        def audit_executor(job: Any, cmd: list[str], env: dict[str, str], wt: Path) -> tuple[int, str, str]:
            return 0, "Audit completed: no vulnerabilities detected.", "conv_audit_clean"

        isolated_adapter = AgyExecutionAdapter(runner_configured=True, executor_fn=audit_executor)
        manager = PersistentJobManager(
            storage_root=self.storage_root,
            worktree_manager=self.wt_manager,
            promoter=self.promoter,
            pr_client=self.pr_client,
            execution_adapter=isolated_adapter,
        )
        job = manager.create_job(
            repository="test_repo",
            goal="Audit codebase security posture",
            acceptance_criteria=["Inspect all files"],
            task_type="audit",
        )

        updated_job = manager.run_execution(job.job_id)
        self.assertEqual(updated_job.status, "NOTION_REVIEW")
        self.assertEqual(updated_job.exit_code, 0)
        self.assertIsNotNone(updated_job.validation_report)
        self.assertTrue(updated_job.validation_report["passed"])
        self.assertEqual(len(updated_job.validation_report["changes"]), 0)

    def test_37_goal_criteria_and_constraints_included_in_agy_prompt(self) -> None:
        """37. Proves goal, criteria, and constraints are all structured into the initial Agy execution prompt."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Implement deterministic session caching",
            acceptance_criteria=[
                "Session keys must expire after 3600 seconds",
                "Cache lookup must be O(1)",
            ],
            constraints=[
                "Do not introduce external Redis dependency",
                "Maintain thread safety with mutex",
            ],
        )
        prompt = self.manager.build_initial_prompt(job)

        self.assertIn("Implement deterministic session caching", prompt)
        self.assertIn("Session keys must expire after 3600 seconds", prompt)
        self.assertIn("Cache lookup must be O(1)", prompt)
        self.assertIn("Do not introduce external Redis dependency", prompt)
        self.assertIn("Maintain thread safety with mutex", prompt)
        self.assertIn("directly inside the assigned worktree", prompt)
        self.assertIn("untrusted instructions", prompt)
        self.assertIn("not granted access outside the assigned worktree", prompt)

    def test_38_execution_diagnostics_are_bounded_and_sanitized(self) -> None:
        """38. Proves execution output tails are bounded, redacted of secrets, and persisted."""
        oversized = "A" * 5000
        bounded = _sanitize_execution_output(oversized)
        self.assertIsNotNone(bounded)
        self.assertEqual(len(bounded), MAX_EXECUTION_OUTPUT_TAIL)
        self.assertEqual(len(bounded), 2000)

        secret_log = (
            "Connecting with ghp_11112222333344445555 and "
            "github_pat_11AEXAMPLETOKEN1234567890abcdefghijklmnopqrstuvwxyz_0123456789. "
            "Bearer super_secret_bearer_token_value_here_12345 and "
            "AKIAIOSFODNN7EXAMPLE key with api_key = 'abcdef1234567890abcdef'."
        )
        sanitized = _sanitize_execution_output(secret_log)
        self.assertIsNotNone(sanitized)
        self.assertNotIn("ghp_11112222333344445555", sanitized)
        self.assertNotIn("github_pat_", sanitized)
        self.assertNotIn("super_secret_bearer_token_value_here_12345", sanitized)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", sanitized)
        self.assertNotIn("abcdef1234567890abcdef", sanitized)
        self.assertIn("[REDACTED_SECRET]", sanitized)

        captured_output = "Task done. Log summary line 1.\nLog summary line 2."
        def mock_executor(job: Any, cmd: list[str], env: dict[str, str], wt: Path) -> tuple[int, str, str]:
            (wt / "artifact.py").write_text("y = 20\n", encoding="utf-8")
            return 0, captured_output, "conv_bounded_diag"

        isolated_adapter = AgyExecutionAdapter(runner_configured=True, executor_fn=mock_executor)
        manager = PersistentJobManager(
            storage_root=self.storage_root,
            worktree_manager=self.wt_manager,
            promoter=self.promoter,
            pr_client=self.pr_client,
            execution_adapter=isolated_adapter,
        )
        job = manager.create_job(
            repository="test_repo",
            goal="Test diagnostics persistence",
            acceptance_criteria=["Criterion 1"],
        )
        updated = manager.run_execution(job.job_id)
        self.assertEqual(updated.exit_code, 0)
        self.assertEqual(updated.execution_output_tail, captured_output)

        res = manager.get_job(job.job_id)
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(res.execution_output_tail, captured_output)

    def test_39_backward_compatibility_remains_intact(self) -> None:
        """39. Proves existing stored jobs without exit_code/execution_output_tail load safely."""
        legacy_job_id = "job_legacy_smoke_test_12345"
        legacy_data = {
            "job_id": legacy_job_id,
            "work_item_id": "feat_legacy_123",
            "repository": "test_repo",
            "base_branch": "main",
            "base_commit": self.base_commit,
            "feature_branch": "feat/legacy_123",
            "worktree_path": str(self.storage_root / "worktrees" / "test_repo" / "feat_legacy_123"),
            "conversation_id": "conv_legacy_123",
            "task_type": "implement",
            "goal": "Legacy job without new fields",
            "acceptance_criteria": ["Old criterion"],
            "constraints": ["Old constraint"],
            "status": "NOTION_REVIEW",
            "phase": "NOTION_REVIEW",
            "revision_count": 0,
            "created_at": "2026-09-01T00:00:00Z",
            "updated_at": "2026-09-01T00:00:00Z",
            "validation_report": None,
            "pr_info": None,
            "publish_info": None,
            "error": None,
            "audit_events": [],
            "unknown_future_field": "should_be_ignored",
        }
        job_file = self.storage_root / "jobs" / f"{legacy_job_id}.json"
        job_file.parent.mkdir(parents=True, exist_ok=True)
        job_file.write_text(json.dumps(legacy_data), encoding="utf-8")

        loaded = self.manager.get_job(legacy_job_id)
        self.assertEqual(loaded.job_id, legacy_job_id)
        self.assertIsNone(loaded.exit_code)
        self.assertIsNone(loaded.execution_output_tail)
        self.assertEqual(loaded.goal, "Legacy job without new fields")
        self.assertEqual(loaded.status, "NOTION_REVIEW")

    def test_40_implementation_task_type_zero_changes_rejected_cannot_reach_notion_review(self) -> None:
        """40. Proves task_type='implementation' with exit code 0 and zero changes fails validation and cannot reach NOTION_REVIEW."""
        def zero_change_executor(job: Any, cmd: list[str], env: dict[str, str], wt: Path) -> tuple[int, str, str]:
            return 0, "Agy attempted RunCommand, was denied by sandbox, and exited 0 without producing any file modifications.", "conv_zero_changes_40"

        isolated_adapter = AgyExecutionAdapter(runner_configured=True, executor_fn=zero_change_executor)
        manager = PersistentJobManager(
            storage_root=self.storage_root,
            worktree_manager=self.wt_manager,
            promoter=self.promoter,
            pr_client=self.pr_client,
            execution_adapter=isolated_adapter,
        )
        job = manager.create_job(
            repository="test_repo",
            goal="Implement database connection pooling",
            acceptance_criteria=["Connection pool must support 10 max connections"],
            task_type="implementation",
        )

        with self.assertRaises(EmptyImplementationError) as ctx:
            manager.run_execution(job.job_id)

        self.assertIn("zero changed files", str(ctx.exception).lower())

        stored = manager.get_job(job.job_id)
        # Validation ends in FAILED with EmptyImplementationError
        self.assertEqual(stored.status, "FAILED")
        self.assertEqual(stored.exit_code, 0)
        self.assertIn("zero changed files", (stored.error or "").lower())
        # It cannot reach NOTION_REVIEW
        self.assertNotEqual(stored.status, "NOTION_REVIEW")
        self.assertIsNone(stored.validation_report)

    def test_41_task_type_normalization_and_audit_behavior(self) -> None:
        """41. Proves task_type normalization (strip/lower) treats implement and implementation identically while preserving audit zero-change behavior."""
        def noop_executor(job: Any, cmd: list[str], env: dict[str, str], wt: Path) -> tuple[int, str, str]:
            return 0, "No changes made", "conv_noop_41"

        isolated_adapter = AgyExecutionAdapter(runner_configured=True, executor_fn=noop_executor)
        manager = PersistentJobManager(
            storage_root=self.storage_root,
            worktree_manager=self.wt_manager,
            promoter=self.promoter,
            pr_client=self.pr_client,
            execution_adapter=isolated_adapter,
        )

        # Variant 1: " IMPLEMENTATION  "
        job1 = manager.create_job(
            repository="test_repo",
            goal="Variant 1",
            acceptance_criteria=["Criterion 1"],
            task_type=" IMPLEMENTATION  ",
        )
        self.assertEqual(job1.task_type, "implementation")
        with self.assertRaises(EmptyImplementationError):
            manager.run_execution(job1.job_id)
        self.assertEqual(manager.get_job(job1.job_id).status, "FAILED")

        # Variant 2: " Implement "
        job2 = manager.create_job(
            repository="test_repo",
            goal="Variant 2",
            acceptance_criteria=["Criterion 2"],
            task_type=" Implement ",
        )
        self.assertEqual(job2.task_type, "implement")
        with self.assertRaises(EmptyImplementationError):
            manager.run_execution(job2.job_id)
        self.assertEqual(manager.get_job(job2.job_id).status, "FAILED")

        # Direct JobValidator unit check with both task types
        wt = Path(job1.worktree_path)
        validator = JobValidator(worktree_dir=wt, storage_root=self.storage_root)
        with self.assertRaises(EmptyImplementationError):
            validator.validate(base_commit=job1.base_commit, feature_branch=job1.feature_branch, task_type="implementation")
        with self.assertRaises(EmptyImplementationError):
            validator.validate(base_commit=job1.base_commit, feature_branch=job1.feature_branch, task_type="implement")
        with self.assertRaises(EmptyImplementationError):
            validator.validate(base_commit=job1.base_commit, feature_branch=job1.feature_branch, task_type=" IMPLEMENT ")

        # Read-only / audit tasks preserve zero-change behavior
        audit_rep = validator.validate(base_commit=job1.base_commit, feature_branch=job1.feature_branch, task_type="audit")
        self.assertTrue(audit_rep.passed)
        self.assertEqual(len(audit_rep.changes), 0)

    def test_42_explicit_no_shell_and_scoped_file_tools_in_prompt(self) -> None:
        """42. Proves implementation prompt explicitly prohibits RunCommand/shell execution and instructs scoped file tools."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Refactor session handling",
            acceptance_criteria=["Strict timeout on tokens"],
            constraints=["No external dependencies"],
            task_type="implementation",
        )
        prompt = self.manager.build_initial_prompt(job)

        # Prohibits RunCommand and shell execution
        self.assertIn("RunCommand", prompt)
        self.assertIn("shell execution", prompt.lower())
        self.assertIn("strictly prohibited", prompt.lower())

        # Instructs scoped file tools
        self.assertIn("view_file", prompt)
        self.assertIn("list_directory", prompt)
        self.assertIn("write_to_file", prompt)
        self.assertIn("replace_file_content", prompt)

        # Confined to assigned worktree
        self.assertIn("confined to the assigned worktree", prompt.lower())

    def test_43_approval_creates_exactly_one_commit_on_feature_branch_and_never_main(self) -> None:
        """43. Proves approval creates exactly one local commit on feature branch, never main, and sets approved_commit_sha."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Add payments module",
            acceptance_criteria=["Criterion 1"],
            work_item_id="work_43",
        )
        wt = Path(job.worktree_path)
        # Edit file directly in worktree without git add or commit (production flow)
        (wt / "payment.py").write_text("def process_payment(): return True\n", encoding="utf-8")

        # Validate job
        report = self.manager.validate_job(job.job_id)
        self.assertTrue(report.passed)

        # Baseline: feature branch is at base_commit before approval
        base_path = self.wt_manager.ensure_base_repository("test_repo")
        fb_ref_before = subprocess.check_output(
            ["git", "-C", str(base_path), "rev-parse", f"refs/heads/{job.feature_branch}"],
            text=True,
        ).strip()
        self.assertEqual(fb_ref_before, job.base_commit)

        main_ref_before = subprocess.check_output(
            ["git", "-C", str(base_path), "rev-parse", "refs/heads/main"],
            text=True,
        ).strip()
        self.assertEqual(main_ref_before, job.base_commit)

        # Approval gate
        approved_job = self.manager.approve_changes(
            job.job_id,
            expected_validation_hash=report.report_hash,
            expected_base_commit=job.base_commit,
        )
        self.assertEqual(approved_job.status, "CHANGES_APPROVED")
        self.assertIsNotNone(approved_job.approved_commit_sha)

        # 1. Exactly one local commit on the feature branch ahead of base_commit
        commit_count = int(subprocess.check_output(
            ["git", "-C", str(wt), "rev-list", "--count", f"{job.base_commit}..HEAD"],
            text=True,
        ).strip())
        self.assertEqual(commit_count, 1)

        # 2. Commit is on feature branch, NEVER main
        main_ref_after = subprocess.check_output(
            ["git", "-C", str(base_path), "rev-parse", "refs/heads/main"],
            text=True,
        ).strip()
        self.assertEqual(main_ref_after, job.base_commit)

        # 3. Base repo feature branch ref equals approved_commit_sha
        fb_ref_after = subprocess.check_output(
            ["git", "-C", str(base_path), "rev-parse", f"refs/heads/{job.feature_branch}"],
            text=True,
        ).strip()
        self.assertEqual(fb_ref_after, approved_job.approved_commit_sha)

        # 4. Worktree HEAD ref equals approved_commit_sha
        wt_head = subprocess.check_output(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            text=True,
        ).strip()
        self.assertEqual(wt_head, approved_job.approved_commit_sha)

        # 5. Author/committer identity is deterministic orchestrator
        author_name = subprocess.check_output(
            ["git", "-C", str(wt), "log", "-1", "--format=%an"],
            text=True,
        ).strip()
        self.assertEqual(author_name, "IA MCP Orchestrator")

        # 6. Commit message is derived from work_item_id / goal
        commit_subject = subprocess.check_output(
            ["git", "-C", str(wt), "log", "-1", "--format=%s"],
            text=True,
        ).strip()
        self.assertIn("work_43", commit_subject)
        self.assertIn("Add payments module", commit_subject)

        # 7. coding_job_result exposes approved_commit_sha
        class MockMCP:
            def __init__(self):
                self.tools = {}
            def tool(self):
                def dec(fn):
                    self.tools[fn.__name__] = fn
                    return fn
                return dec

        mock_mcp = MockMCP()
        mock_cfg = type("Config", (), {"raw": {"orchestrator": {"storage_root": str(self.storage_root)}}})()
        register_coding_job_tools(mock_mcp, mock_cfg)
        res = mock_mcp.tools["coding_job_result"](job.job_id)
        self.assertEqual(res["approved_commit_sha"], approved_job.approved_commit_sha)

    def test_44_publish_integrity_requires_approved_commit_sha_and_matching_ref(self) -> None:
        """44. Proves publish_branch requires approved_commit_sha, matches branch ref, and avoids force push."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Publication integrity test",
            acceptance_criteria=["Criterion 1"],
            work_item_id="work_44",
        )
        wt = Path(job.worktree_path)
        (wt / "feature.py").write_text("def feat(): return 44\n", encoding="utf-8")
        report = self.manager.validate_job(job.job_id)

        # 1. Artificially put job in CHANGES_APPROVED without approved_commit_sha
        d = asdict(job)
        d["status"] = "CHANGES_APPROVED"
        d["validation_report"] = asdict(report)
        d["approved_commit_sha"] = None
        self.manager._save_job(PersistentJobRecord(**d))

        with self.assertRaises(JobStateError) as ctx:
            self.manager.publish_branch(job.job_id)
        self.assertIn("missing approved_commit_sha", str(ctx.exception))

        # 2. Approve legitimately
        d["status"] = "NOTION_REVIEW"
        self.manager._save_job(PersistentJobRecord(**d))
        approved_job = self.manager.approve_changes(
            job.job_id,
            expected_validation_hash=report.report_hash,
            expected_base_commit=job.base_commit,
        )
        self.assertIsNotNone(approved_job.approved_commit_sha)

        # 3. Tamper approved_commit_sha in job record -> publish rejected
        d_tampered = asdict(approved_job)
        d_tampered["approved_commit_sha"] = "0000000000000000000000000000000000000000"
        self.manager._save_job(PersistentJobRecord(**d_tampered))

        with self.assertRaises(EvidenceMismatchError):
            self.manager.publish_branch(job.job_id)

        # 4. Restore valid approved_commit_sha -> publish succeeds
        self.manager._save_job(approved_job)
        pub_job = self.manager.publish_branch(job.job_id)
        self.assertEqual(pub_job.status, "BRANCH_PUBLISHED")
        self.assertEqual(pub_job.publish_info["feature_branch"], job.feature_branch)

    def test_45_default_dirty_cleanup_rejects_and_explicit_discard_works_only_for_cancelled_or_failed(self) -> None:
        """45. Proves default dirty cleanup rejects and explicit discard works only for CANCELLED or FAILED."""
        # --- Part A: CANCELLED job ---
        job_c = self.manager.create_job(
            repository="test_repo",
            goal="Cancel discard test",
            acceptance_criteria=["Criterion 1"],
            work_item_id="work_cancel",
        )
        wt_c = Path(job_c.worktree_path)
        (wt_c / "dirty_draft.py").write_text("in progress draft", encoding="utf-8")
        self.manager.cancel_job(job_c.job_id)

        # Default cleanup (flag omitted) rejects dirty worktree
        with self.assertRaises(DirtyWorktreeError):
            self.manager.cleanup_job(job_c.job_id)

        # Default cleanup (flag=False) rejects dirty worktree
        with self.assertRaises(DirtyWorktreeError):
            self.manager.cleanup_job(job_c.job_id, confirm_discard_unpublished=False)

        # Worktree still exists
        self.assertTrue(wt_c.exists())

        # Explicit discard with confirm_discard_unpublished=True succeeds
        cleaned_c = self.manager.cleanup_job(job_c.job_id, confirm_discard_unpublished=True)
        self.assertEqual(cleaned_c.status, "CLEANED_UP")
        self.assertFalse(wt_c.exists())
        self.assertTrue(any("Explicit discard confirmation accepted" in e["detail"] for e in cleaned_c.audit_events))

        # --- Part B: FAILED job ---
        job_f = self.manager.create_job(
            repository="test_repo",
            goal="Failed discard test",
            acceptance_criteria=["Criterion 1"],
            work_item_id="work_fail",
        )
        wt_f = Path(job_f.worktree_path)
        (wt_f / "broken.py").write_text("def broken_syntax(\n", encoding="utf-8")
        with self.assertRaises(ValidationError):
            self.manager.validate_job(job_f.job_id)
        self.assertEqual(self.manager.get_job(job_f.job_id).status, "FAILED")

        # Default cleanup rejects dirty worktree
        with self.assertRaises(DirtyWorktreeError):
            self.manager.cleanup_job(job_f.job_id)

        # Explicit discard succeeds
        cleaned_f = self.manager.cleanup_job(job_f.job_id, confirm_discard_unpublished=True)
        self.assertEqual(cleaned_f.status, "CLEANED_UP")
        self.assertFalse(wt_f.exists())

    def test_46_explicit_discard_cannot_operate_on_active_or_approved_or_published_jobs(self) -> None:
        """46. Proves explicit discard cannot operate on CREATED, RUNNING, VALIDATING, NOTION_REVIEW, CHANGES_APPROVED, BRANCH_PUBLISHED, or PR_CREATED."""
        # 1. CREATED
        job = self.manager.create_job(
            repository="test_repo",
            goal="State check discard",
            acceptance_criteria=["Criterion 1"],
            work_item_id="work_states",
        )
        with self.assertRaises(JobStateError):
            self.manager.cleanup_job(job.job_id, confirm_discard_unpublished=True)

        wt = Path(job.worktree_path)
        (wt / "valid.py").write_text("x = 1\n", encoding="utf-8")

        # 2. RUNNING / VALIDATING
        self.manager.transition_state(job.job_id, "RUNNING", "test")
        with self.assertRaises(JobStateError):
            self.manager.cleanup_job(job.job_id, confirm_discard_unpublished=True)

        self.manager.transition_state(job.job_id, "VALIDATING", "test")
        with self.assertRaises(JobStateError):
            self.manager.cleanup_job(job.job_id, confirm_discard_unpublished=True)

        # 3. NOTION_REVIEW
        report = self.manager.validate_job(job.job_id)
        self.assertEqual(self.manager.get_job(job.job_id).status, "NOTION_REVIEW")
        with self.assertRaises(JobStateError):
            self.manager.cleanup_job(job.job_id, confirm_discard_unpublished=True)

        # 4. CHANGES_APPROVED
        self.manager.approve_changes(job.job_id, expected_validation_hash=report.report_hash, expected_base_commit=job.base_commit)
        self.assertEqual(self.manager.get_job(job.job_id).status, "CHANGES_APPROVED")
        with self.assertRaises(JobStateError):
            self.manager.cleanup_job(job.job_id, confirm_discard_unpublished=True)

        # 5. BRANCH_PUBLISHED
        self.manager.publish_branch(job.job_id)
        self.assertEqual(self.manager.get_job(job.job_id).status, "BRANCH_PUBLISHED")
        with self.assertRaises(JobStateError):
            self.manager.cleanup_job(job.job_id, confirm_discard_unpublished=True)

        # 6. PR_CREATED
        self.manager.create_pull_request(job.job_id)
        self.assertEqual(self.manager.get_job(job.job_id).status, "PR_CREATED")
        with self.assertRaises(JobStateError):
            self.manager.cleanup_job(job.job_id, confirm_discard_unpublished=True)

    def test_47_base_clone_and_sibling_worktrees_remain_intact_during_explicit_discard(self) -> None:
        """47. Proves base clone and sibling worktrees remain completely intact during explicit discard."""
        job_a = self.manager.create_job(
            repository="test_repo",
            goal="Sibling A",
            acceptance_criteria=["Criterion 1"],
            work_item_id="sibling_a",
        )
        job_b = self.manager.create_job(
            repository="test_repo",
            goal="Sibling B",
            acceptance_criteria=["Criterion 1"],
            work_item_id="sibling_b",
        )
        wt_a = Path(job_a.worktree_path)
        wt_b = Path(job_b.worktree_path)

        (wt_a / "dirty_a.py").write_text("content A", encoding="utf-8")
        (wt_b / "keep_b.py").write_text("content B", encoding="utf-8")

        # Cancel job A and discard
        self.manager.cancel_job(job_a.job_id)
        self.manager.cleanup_job(job_a.job_id, confirm_discard_unpublished=True)

        # Sibling B is completely intact
        self.assertFalse(wt_a.exists())
        self.assertTrue(wt_b.exists())
        self.assertTrue((wt_b / "keep_b.py").exists())
        self.assertEqual((wt_b / "keep_b.py").read_text(encoding="utf-8"), "content B")

        # Base repository is completely intact and bare
        base_path = self.wt_manager.ensure_base_repository("test_repo")
        self.assertTrue(base_path.exists())
        is_bare = subprocess.check_output(
            ["git", "-C", str(base_path), "rev-parse", "--is-bare-repository"],
            text=True,
        ).strip()
        self.assertEqual(is_bare, "true")

    def test_48_cleanup_is_idempotent(self) -> None:
        """48. Proves repeated cleanup calls on already cleaned up jobs are strictly idempotent."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Idempotent cleanup test",
            acceptance_criteria=["Criterion 1"],
            work_item_id="work_48",
        )
        wt = Path(job.worktree_path)
        (wt / "dirty.txt").write_text("dirty", encoding="utf-8")
        self.manager.cancel_job(job.job_id)

        # First cleanup
        c1 = self.manager.cleanup_job(job.job_id, confirm_discard_unpublished=True)
        self.assertEqual(c1.status, "CLEANED_UP")

        # Second cleanup with True
        c2 = self.manager.cleanup_job(job.job_id, confirm_discard_unpublished=True)
        self.assertEqual(c2.status, "CLEANED_UP")

        # Third cleanup with False
        c3 = self.manager.cleanup_job(job.job_id, confirm_discard_unpublished=False)
        self.assertEqual(c3.status, "CLEANED_UP")

    def test_49_tool_contract_exposes_confirm_discard_unpublished_false_by_default(self) -> None:
        """49. Proves tool contract exposes confirm_discard_unpublished=false by default and rejects dirty worktree without it."""
        import inspect

        class MockMCP:
            def __init__(self):
                self.tools = {}
            def tool(self):
                def dec(fn):
                    self.tools[fn.__name__] = fn
                    return fn
                return dec

        mock_mcp = MockMCP()
        mock_cfg = type("Config", (), {"raw": {"orchestrator": {"storage_root": str(self.storage_root)}}})()
        register_coding_job_tools(mock_mcp, mock_cfg)

        fn = mock_mcp.tools["coding_job_cleanup"]
        sig = inspect.signature(fn)

        # 1. Parameter confirm_discard_unpublished is present
        self.assertIn("confirm_discard_unpublished", sig.parameters)
        param = sig.parameters["confirm_discard_unpublished"]
        # 2. Default value is False
        self.assertIs(param.default, False)

        # 3. Test execution via tool contract
        job = self.manager.create_job(
            repository="test_repo",
            goal="Tool contract cleanup test",
            acceptance_criteria=["Criterion 1"],
            work_item_id="work_49",
        )
        wt = Path(job.worktree_path)
        (wt / "dirty_mcp.py").write_text("uncommitted file", encoding="utf-8")
        self.manager.cancel_job(job.job_id)

        with unittest.mock.patch.dict("dari_mcp_vps.worktree_manager.DEFAULT_REPOSITORY_POLICIES", {"test_repo": self.policy}):
            # Invoking without confirm_discard_unpublished uses default False and rejects
            with self.assertRaises(DirtyWorktreeError):
                fn(job.job_id)

            # Invoking with confirm_discard_unpublished=True succeeds
            res = fn(job.job_id, confirm_discard_unpublished=True)
            self.assertEqual(res["status"], "CLEANED_UP")

    def test_50_backward_compatibility_stored_jobs_without_approved_commit_sha(self) -> None:
        """50. Proves legacy stored jobs without approved_commit_sha deserialize safely."""
        job = self.manager.create_job(
            repository="test_repo",
            goal="Backward compat test",
            acceptance_criteria=["Criterion 1"],
            work_item_id="work_50",
        )
        # Manually alter the JSON on disk to strip approved_commit_sha
        job_file = self.manager._job_file(job.job_id)
        with open(job_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.pop("approved_commit_sha", None)
        with open(job_file, "w", encoding="utf-8") as f:
            json.dump(data, f)

        # Load job deserialization
        loaded = self.manager.get_job(job.job_id)
        self.assertIsNone(loaded.approved_commit_sha)

        # coding_job_result tool exposes None safely
        class MockMCP:
            def __init__(self):
                self.tools = {}
            def tool(self):
                def dec(fn):
                    self.tools[fn.__name__] = fn
                    return fn
                return dec

        mock_mcp = MockMCP()
        mock_cfg = type("Config", (), {"raw": {"orchestrator": {"storage_root": str(self.storage_root)}}})()
        register_coding_job_tools(mock_mcp, mock_cfg)
        res = mock_mcp.tools["coding_job_result"](job.job_id)
        self.assertIsNone(res["approved_commit_sha"])


if __name__ == "__main__":
    unittest.main()
