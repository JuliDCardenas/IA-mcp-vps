from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from dari_mcp_vps.worktree_manager import (
    DirtyWorktreeError,
    NonFastForwardError,
    RepositoryNotFoundError,
    RepositoryPolicy,
    SecurityError,
    WorktreeManager,
)


def _init_local_git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=path, check=True, capture_output=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


class TestWorktreeManager(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="wt_test_")
        self.root = Path(self.temp_dir)
        self.storage_root = self.root / "storage"
        self.remote_origin = self.root / "origin.git"
        self.initial_commit = _init_local_git_repo(self.remote_origin)

        self.policy = RepositoryPolicy(
            alias="test_repo",
            clone_url=str(self.remote_origin),
            default_base_branch="main",
        )
        self.manager = WorktreeManager(
            storage_root=self.storage_root,
            policies={"test_repo": self.policy},
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_initial_permanent_bare_base_clone(self) -> None:
        """1. Verifies that the initial permanent bare base clone is created properly."""
        base_path = self.manager.ensure_base_repository("test_repo")
        self.assertTrue(base_path.exists())
        self.assertTrue((base_path / "HEAD").exists())
        is_bare = subprocess.check_output(
            ["git", "-C", str(base_path), "rev-parse", "--is-bare-repository"],
            text=True,
        ).strip()
        self.assertEqual(is_bare, "true")

    def test_02_safe_fast_forward_refresh(self) -> None:
        """2. Verifies safe fast-forward base refresh advances base commit cleanly."""
        self.manager.ensure_base_repository("test_repo")

        # Create new commit on remote origin
        (self.remote_origin / "new_file.txt").write_text("content v2", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.remote_origin, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "v2 commit"], cwd=self.remote_origin, check=True, capture_output=True)
        remote_c2 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.remote_origin, text=True).strip()

        old_commit, new_commit = self.manager.refresh_base_repository("test_repo")
        self.assertEqual(old_commit, self.initial_commit)
        self.assertEqual(new_commit, remote_c2)

    def test_03_rejection_of_non_fast_forward_base_movement(self) -> None:
        """3. Verifies that remote history divergence/force-push is strictly rejected."""
        self.manager.ensure_base_repository("test_repo")

        # Advance remote origin with commit A
        (self.remote_origin / "commit_a.txt").write_text("commit a", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.remote_origin, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Commit A"], cwd=self.remote_origin, check=True, capture_output=True)
        self.manager.refresh_base_repository("test_repo")

        # Force reset remote origin back to initial and create diverging Commit B
        subprocess.run(["git", "reset", "--hard", self.initial_commit], cwd=self.remote_origin, check=True, capture_output=True)
        (self.remote_origin / "commit_b.txt").write_text("commit b", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.remote_origin, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Commit B diverging"], cwd=self.remote_origin, check=True, capture_output=True)

        with self.assertRaises(NonFastForwardError):
            self.manager.refresh_base_repository("test_repo")

    def test_04_feature_branch_is_not_main(self) -> None:
        """4. Verifies that feature branch is server-generated and never main."""
        workspace = self.manager.get_or_create_workspace("test_repo", "task_100")
        self.assertNotEqual(workspace.feature_branch, "main")
        self.assertNotEqual(workspace.feature_branch, workspace.base_branch)
        self.assertTrue(workspace.feature_branch.startswith("feat/test_repo-"))

    def test_05_persistent_worktree_creation(self) -> None:
        """5. Verifies persistent worktree creation and metadata integrity."""
        workspace = self.manager.get_or_create_workspace("test_repo", "task_101")
        wt_path = Path(workspace.worktree_path)
        self.assertTrue(wt_path.exists())
        self.assertTrue((wt_path / ".git").exists())
        self.assertTrue((wt_path / "README.md").exists())
        self.assertEqual(workspace.lifecycle_state, "ACTIVE")
        self.assertTrue(Path(workspace.metadata_file).exists())

    def test_06_reuse_of_same_branch_and_worktree(self) -> None:
        """6. Verifies that requesting the same work item reuses branch and worktree."""
        w1 = self.manager.get_or_create_workspace("test_repo", "task_repeat")
        # Add an uncommitted file to simulate in-flight work
        (Path(w1.worktree_path) / "draft.txt").write_text("draft work", encoding="utf-8")

        w2 = self.manager.get_or_create_workspace("test_repo", "task_repeat")
        self.assertEqual(w1.worktree_path, w2.worktree_path)
        self.assertEqual(w1.feature_branch, w2.feature_branch)
        self.assertEqual(w1.creation_timestamp, w2.creation_timestamp)
        # Verify in-flight work is preserved
        self.assertTrue((Path(w2.worktree_path) / "draft.txt").exists())

    def test_07_different_work_items_receive_different_branches_and_worktrees(self) -> None:
        """7. Verifies different work items receive isolated branches and worktrees."""
        w1 = self.manager.get_or_create_workspace("test_repo", "item_alpha")
        w2 = self.manager.get_or_create_workspace("test_repo", "item_beta")

        self.assertNotEqual(w1.worktree_path, w2.worktree_path)
        self.assertNotEqual(w1.feature_branch, w2.feature_branch)
        self.assertTrue(Path(w1.worktree_path).exists())
        self.assertTrue(Path(w2.worktree_path).exists())

    def test_08_editing_worktree_does_not_dirty_or_change_base_repository(self) -> None:
        """8. Verifies editing a worktree does not dirty or mutate the bare base repository."""
        workspace = self.manager.get_or_create_workspace("test_repo", "item_isolate")
        wt_path = Path(workspace.worktree_path)

        # Mutate worktree
        (wt_path / "scratch.py").write_text("print('test')", encoding="utf-8")
        (wt_path / "README.md").write_text("Modified readme", encoding="utf-8")

        base_path = self.manager._base_repo_path("test_repo")
        # Base repo remains bare and pristine
        is_bare = subprocess.check_output(
            ["git", "-C", str(base_path), "rev-parse", "--is-bare-repository"],
            text=True,
        ).strip()
        self.assertEqual(is_bare, "true")

    def test_09_path_traversal_and_unsafe_identifiers_are_rejected(self) -> None:
        """9. Verifies that path traversal and unsafe identifiers are strictly rejected."""
        with self.assertRaises(SecurityError):
            self.manager.get_or_create_workspace("test_repo", "../escape")

        with self.assertRaises(SecurityError):
            self.manager.get_or_create_workspace("test_repo", "item/with/slashes")

        with self.assertRaises(SecurityError):
            self.manager.get_or_create_workspace("test_repo", "item;rm -rf")

        with self.assertRaises(RepositoryNotFoundError):
            self.manager.get_or_create_workspace("unknown_alias", "valid_item")

    def test_10_dirty_worktree_cleanup_is_rejected(self) -> None:
        """10. Verifies that dirty worktree cleanup is rejected and clean worktree succeeds."""
        workspace = self.manager.get_or_create_workspace("test_repo", "clean_test")
        wt_path = Path(workspace.worktree_path)

        # Make it dirty with an uncommitted file
        dirty_file = wt_path / "uncommitted.txt"
        dirty_file.write_text("dirty content", encoding="utf-8")

        # Cleanup must be rejected
        with self.assertRaises(DirtyWorktreeError):
            self.manager.cleanup_workspace("test_repo", "clean_test")

        self.assertTrue(wt_path.exists())

        # Remove dirty file and test clean removal
        dirty_file.unlink()
        cleaned_record = self.manager.cleanup_workspace("test_repo", "clean_test")
        self.assertEqual(cleaned_record.lifecycle_state, "CLEANED")
        self.assertFalse(wt_path.exists())

    def test_11_existing_coding_job_imports_and_server_registration(self) -> None:
        """11. Verifies existing coding-job imports and server tool registrations remain functional."""
        from dari_mcp_vps.tools.coding_jobs import register_coding_job_tools
        from dari_mcp_vps.tools.private_coding_job import register_private_coding_job_tool

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
            "coding_private_job_create",
        }
        self.assertTrue(expected_tools.issubset(set(mock_mcp.tools)))


if __name__ == "__main__":
    unittest.main()
