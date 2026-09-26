from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from typing import Any

from dari_mcp_vps.docker_runner import (
    ALLOWLISTED_AGY_IMAGES,
    ALLOWLISTED_AGY_VOLUMES,
    ALLOWLISTED_JOB_NETWORKS,
    DEFAULT_AGY_HOME_VOLUME,
    DEFAULT_AGY_IMAGE,
    DEFAULT_JOB_NETWORK,
    DeploymentBlockedError,
    DockerAgyJobRunner,
    DockerRunnerConfig,
    DockerRunnerError,
    _demux_docker_logs,
    _redact_sensitive,
    resolve_worktree_bind_source,
)
from dari_mcp_vps.persistent_job import PersistentJobManager
from dari_mcp_vps.tools.coding_jobs import register_coding_job_tools
from dari_mcp_vps.worktree_manager import RepositoryPolicy, SecurityError, WorktreeManager


class MockDockerTransport:
    """In-memory mock transport for Docker Engine REST API (no real containers)."""

    def __init__(self) -> None:
        self.created_containers: list[dict[str, Any]] = []
        self.started_containers: list[str] = []
        self.stopped_containers: list[str] = []
        self.deleted_containers: list[str] = []
        self.log_responses: dict[str, bytes] = {}
        self.wait_responses: dict[str, dict[str, Any]] = {}
        self.list_containers_response: list[dict[str, Any]] = []
        self.next_cid = 100
        self.raise_on_wait: Exception | None = None

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> bytes:
        if method == "POST" and "/start" in path:
            cid = path.split("/")[2]
            self.started_containers.append(cid)
            return b""
        elif method == "POST" and "/stop" in path:
            cid = path.split("/")[2]
            self.stopped_containers.append(cid)
            return b""
        elif method == "DELETE" and "/containers/" in path:
            cid = path.split("/")[2].split("?")[0]
            self.deleted_containers.append(cid)
            return b""
        elif method == "GET" and "/logs" in path:
            cid = path.split("/")[2]
            raw = self.log_responses.get(cid, b'{"conversation_id": "conv_mock_123"}\n')
            header = bytes([1, 0, 0, 0]) + len(raw).to_bytes(4, "big")
            return header + raw
        return b""

    def json_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> Any:
        if method == "POST" and path.startswith("/containers/create"):
            cid = f"cid_{self.next_cid}"
            self.next_cid += 1
            record = {
                "cid": cid,
                "path": path,
                "payload": payload,
            }
            self.created_containers.append(record)
            return {"Id": cid, "Warnings": []}
        elif method == "POST" and "/wait" in path:
            if self.raise_on_wait:
                raise self.raise_on_wait
            cid = path.split("/")[2]
            return self.wait_responses.get(cid, {"StatusCode": 0})
        elif method == "GET" and path.startswith("/containers/json"):
            return self.list_containers_response
        return None


class TestDockerRunner(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="docker_runner_test_")
        self.root = Path(self.temp_dir)
        self.container_storage_root = self.root / "var" / "lib" / "coding-jobs"
        self.host_storage_root = Path("/home/ubuntu/.local/share/ia-mcp-vps/coding-jobs")

        self.container_storage_root.mkdir(parents=True, exist_ok=True)
        (self.container_storage_root / "worktrees" / "ia_mcp_vps" / "feat_test").mkdir(parents=True, exist_ok=True)

        self.mock_transport = MockDockerTransport()
        self.runner_config = DockerRunnerConfig(
            enabled=True,
            container_storage_root=self.container_storage_root,
            host_storage_root=self.host_storage_root,
            agy_image=DEFAULT_AGY_IMAGE,
            agy_home_volume=DEFAULT_AGY_HOME_VOLUME,
            job_network=DEFAULT_JOB_NETWORK,
            timeout_seconds=1200,
        )
        self.runner = DockerAgyJobRunner(
            config=self.runner_config,
            client=self.mock_transport,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_host_and_container_roots_are_different_and_exact_bind_translation(self) -> None:
        """1. Proves host and container storage roots are distinct and translated exactly."""
        self.assertNotEqual(self.container_storage_root, self.host_storage_root)
        wt = self.container_storage_root / "worktrees" / "ia_mcp_vps" / "feat_test"
        exit_code, output, conv_id = self.runner.run_job(
            job_id="job_001",
            work_item_id="feat_test",
            repository="ia_mcp_vps",
            worktree_path=wt,
            prompt="Implement feature",
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(self.mock_transport.created_containers), 1)
        created = self.mock_transport.created_containers[0]
        binds = created["payload"]["HostConfig"]["Binds"]
        expected_host_worktree = self.host_storage_root / "worktrees" / "ia_mcp_vps" / "feat_test"
        self.assertEqual(
            binds,
            [
                f"{expected_host_worktree}:/workspace:rw",
                f"{DEFAULT_AGY_HOME_VOLUME}:/home/agy:rw",
            ],
        )
        # Inside the container, destination is strictly /workspace:rw, never /home/ubuntu
        self.assertEqual(created["payload"]["WorkingDir"], "/workspace")
        self.assertTrue(binds[0].endswith(":/workspace:rw"))
        self.assertNotIn(":/home/ubuntu", binds[0])

    def test_02_docker_socket_is_never_mounted(self) -> None:
        """2. Docker socket is never mounted into the job container."""
        wt = self.container_storage_root / "worktrees" / "ia_mcp_vps" / "feat_test"
        self.runner.run_job(
            job_id="job_002",
            work_item_id="feat_test",
            repository="ia_mcp_vps",
            worktree_path=wt,
            prompt="No docker socket",
        )
        created = self.mock_transport.created_containers[0]
        binds_str = str(created["payload"]["HostConfig"]["Binds"])
        self.assertNotIn("docker.sock", binds_str)
        self.assertNotIn("/var/run/docker.sock", binds_str)

    def test_03_host_stack_paths_never_mounted_and_no_parent_host_dir(self) -> None:
        """3. Host stack paths and parent host directories are never mounted."""
        wt = self.container_storage_root / "worktrees" / "ia_mcp_vps" / "feat_test"
        self.runner.run_job(
            job_id="job_003",
            work_item_id="feat_test",
            repository="ia_mcp_vps",
            worktree_path=wt,
            prompt="No host stacks",
        )
        created = self.mock_transport.created_containers[0]
        binds = created["payload"]["HostConfig"]["Binds"]
        binds_str = str(binds)
        self.assertNotIn("/mnt/stacks", binds_str)
        self.assertNotIn("/config", binds_str)
        self.assertNotIn("/etc", binds_str)
        # Parent host root must not be mounted directly
        for bind in binds:
            host_src = bind.split(":")[0]
            self.assertNotEqual(host_src, str(self.host_storage_root))
            self.assertNotEqual(host_src, "/home/ubuntu")
            self.assertNotEqual(host_src, "/home/ubuntu/.local/share/ia-mcp-vps")

    def test_04_bases_and_siblings_never_mounted(self) -> None:
        """4. Base repository and sibling worktrees are never mounted."""
        base_path = self.container_storage_root / "bases" / "ia_mcp_vps.git"
        base_path.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(SecurityError):
            resolve_worktree_bind_source(
                container_worktree=base_path,
                container_storage_root=self.container_storage_root,
                host_storage_root=self.host_storage_root,
            )

        with self.assertRaises(SecurityError):
            resolve_worktree_bind_source(
                container_worktree=self.container_storage_root / "worktrees",
                container_storage_root=self.container_storage_root,
                host_storage_root=self.host_storage_root,
            )

        with self.assertRaises(SecurityError):
            resolve_worktree_bind_source(
                container_worktree=self.container_storage_root / "worktrees" / "ia_mcp_vps",
                container_storage_root=self.container_storage_root,
                host_storage_root=self.host_storage_root,
            )

    def test_05_security_options_and_limits_present(self) -> None:
        """5. User, read-only root, capabilities, no-new-privileges, PIDs, CPU and memory limits are present."""
        wt = self.container_storage_root / "worktrees" / "ia_mcp_vps" / "feat_test"
        self.runner.run_job(
            job_id="job_005",
            work_item_id="feat_test",
            repository="ia_mcp_vps",
            worktree_path=wt,
            prompt="Security options",
        )
        created = self.mock_transport.created_containers[0]["payload"]
        self.assertEqual(created["User"], "10001:10001")
        host_cfg = created["HostConfig"]
        self.assertTrue(host_cfg["ReadonlyRootfs"])
        self.assertEqual(host_cfg["CapDrop"], ["ALL"])
        self.assertEqual(host_cfg["SecurityOpt"], ["no-new-privileges:true"])
        self.assertEqual(host_cfg["PidsLimit"], 256)
        self.assertEqual(host_cfg["Memory"], 2147483648)
        self.assertEqual(host_cfg["NanoCpus"], 2000000000)

    def test_06_no_ports_published_or_exposed(self) -> None:
        """6. No ports are published or exposed."""
        wt = self.container_storage_root / "worktrees" / "ia_mcp_vps" / "feat_test"
        self.runner.run_job(
            job_id="job_006",
            work_item_id="feat_test",
            repository="ia_mcp_vps",
            worktree_path=wt,
            prompt="No ports",
        )
        payload = self.mock_transport.created_containers[0]["payload"]
        host_cfg = payload["HostConfig"]
        self.assertEqual(host_cfg["PortBindings"], {})
        self.assertFalse(host_cfg["PublishAllPorts"])
        self.assertNotIn("ExposedPorts", payload)

    def test_07_fixed_agy_command_and_workingdir_used(self) -> None:
        """7. Fixed Agy command and /workspace WorkingDir are used."""
        wt = self.container_storage_root / "worktrees" / "ia_mcp_vps" / "feat_test"
        self.runner.run_job(
            job_id="job_007",
            work_item_id="feat_test",
            repository="ia_mcp_vps",
            worktree_path=wt,
            prompt="Initial prompt",
            is_revision=False,
        )
        created_init = self.mock_transport.created_containers[0]["payload"]
        self.assertEqual(created_init["WorkingDir"], "/workspace")
        self.assertEqual(
            created_init["Cmd"],
            ["agy", "-p", "Initial prompt", "--mode=accept-edits", "--sandbox", "--print-timeout", "20m", "--output-format", "json"],
        )

        self.runner.run_job(
            job_id="job_007b",
            work_item_id="feat_test",
            repository="ia_mcp_vps",
            worktree_path=wt,
            prompt="Revision prompt",
            is_revision=True,
            conversation_id="conv_real_42",
        )
        created_rev = self.mock_transport.created_containers[1]["payload"]
        self.assertEqual(created_rev["WorkingDir"], "/workspace")
        self.assertEqual(
            created_rev["Cmd"],
            ["agy", "--conversation", "conv_real_42", "-p", "Revision prompt", "--mode=accept-edits", "--sandbox", "--print-timeout", "20m", "--output-format", "json"],
        )

    def test_08_environment_minimal_and_contains_no_publication_credentials(self) -> None:
        """8. Environment is minimal and contains no publication credentials."""
        wt = self.container_storage_root / "worktrees" / "ia_mcp_vps" / "feat_test"
        self.runner.run_job(
            job_id="job_008",
            work_item_id="feat_test",
            repository="ia_mcp_vps",
            worktree_path=wt,
            prompt="Env check",
        )
        env = self.mock_transport.created_containers[0]["payload"]["Env"]
        env_dict = dict(item.split("=", 1) for item in env)
        self.assertEqual(env_dict["HOME"], "/home/agy")
        self.assertIn("/home/agy/.local/bin", env_dict["PATH"])
        self.assertEqual(env_dict["LANG"], "C.UTF-8")
        self.assertNotIn("GITHUB_TOKEN", env_dict)
        self.assertNotIn("GH_TOKEN", env_dict)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env_dict)
        self.assertNotIn("SSH_AUTH_SOCK", env_dict)

    def test_09_invalid_or_escaped_worktree_paths_rejected(self) -> None:
        """9. Invalid or escaped worktree paths are rejected."""
        escaped = self.container_storage_root / "worktrees" / ".." / "bases"
        with self.assertRaises(SecurityError):
            resolve_worktree_bind_source(
                container_worktree=escaped,
                container_storage_root=self.container_storage_root,
                host_storage_root=self.host_storage_root,
            )

        with self.assertRaises(SecurityError):
            resolve_worktree_bind_source(
                container_worktree=Path("/etc/shadow"),
                container_storage_root=self.container_storage_root,
                host_storage_root=self.host_storage_root,
            )

    def test_10_container_image_allowlisted_and_cannot_come_from_job_input(self) -> None:
        """10. Container image is allowlisted and cannot come from job input."""
        bad_config = DockerRunnerConfig(
            enabled=True,
            container_storage_root=self.container_storage_root,
            host_storage_root=self.host_storage_root,
            agy_image="untrusted/malicious:latest",
        )
        with self.assertRaises(SecurityError):
            DockerAgyJobRunner(config=bad_config, client=self.mock_transport)

        for img in ALLOWLISTED_AGY_IMAGES:
            cfg = DockerRunnerConfig(
                enabled=True,
                container_storage_root=self.container_storage_root,
                host_storage_root=self.host_storage_root,
                agy_image=img,
            )
            runner = DockerAgyJobRunner(config=cfg, client=self.mock_transport)
            self.assertEqual(runner.config.agy_image, img)

    def test_11_cancellation_targets_only_matching_labeled_container(self) -> None:
        """11. Cancellation targets only the matching labeled container."""
        self.mock_transport.list_containers_response = [
            {
                "Id": "cid_target_11",
                "Names": ["/agy-job-job_target_11"],
                "Labels": {"ia_mcp_vps.job_id": "job_target_11"},
            },
            {
                "Id": "cid_other_22",
                "Names": ["/agy-job-job_other_22"],
                "Labels": {"ia_mcp_vps.job_id": "job_other_22"},
            },
        ]
        cancelled = self.runner.cancel_job("job_target_11")
        self.assertTrue(cancelled)
        self.assertIn("cid_target_11", self.mock_transport.stopped_containers)
        self.assertIn("cid_target_11", self.mock_transport.deleted_containers)
        self.assertNotIn("cid_other_22", self.mock_transport.stopped_containers)
        self.assertNotIn("cid_other_22", self.mock_transport.deleted_containers)

    def test_12_timeout_stops_and_removes_container(self) -> None:
        """12. Timeout stops and removes the container."""
        wt = self.container_storage_root / "worktrees" / "ia_mcp_vps" / "feat_test"
        self.mock_transport.raise_on_wait = socket.timeout("Timed out waiting for container")
        with self.assertRaises(TimeoutError):
            self.runner.run_job(
                job_id="job_012",
                work_item_id="feat_test",
                repository="ia_mcp_vps",
                worktree_path=wt,
                prompt="Timeout test",
            )
        self.assertEqual(len(self.mock_transport.stopped_containers), 1)
        self.assertEqual(len(self.mock_transport.deleted_containers), 1)

    def test_13_output_is_bounded_and_sanitized(self) -> None:
        """13. Output is bounded and sanitized."""
        wt = self.container_storage_root / "worktrees" / "ia_mcp_vps" / "feat_test"
        leak_text = (
            "token=ghp_ABC12345678901234567890123456789012\n"
            "pat=github_pat_11AAAAAA0000000000000000000000000000000000000000000000000000000000\n"
            '{"conversation_id": "conv_sanitized_99"}\n'
        ).encode("utf-8")
        self.mock_transport.log_responses["cid_100"] = leak_text

        exit_code, sanitized_output, conv_id = self.runner.run_job(
            job_id="job_013",
            work_item_id="feat_test",
            repository="ia_mcp_vps",
            worktree_path=wt,
            prompt="Sanitize test",
        )
        self.assertNotIn("ghp_ABC", sanitized_output)
        self.assertNotIn("github_pat_11", sanitized_output)
        self.assertIn("[REDACTED]", sanitized_output)
        self.assertEqual(conv_id, "conv_sanitized_99")

    def test_14_local_execution_inside_mcp_container_is_impossible(self) -> None:
        """14. Local execution inside the MCP container is impossible."""
        import inspect
        from dari_mcp_vps import persistent_job
        source = inspect.getsource(persistent_job)
        self.assertNotIn("subprocess.Popen", source)
        self.assertNotIn("subprocess.run", source)

    def test_15_persistent_mode_fails_closed_when_runner_or_storage_absent(self) -> None:
        """15. Persistent mode fails closed when runner/storage/network configuration is absent."""
        unconfigured_runner = DockerAgyJobRunner(
            config=DockerRunnerConfig(enabled=False, host_storage_root=None),
            client=self.mock_transport,
        )
        self.assertFalse(unconfigured_runner.is_configured)
        wt = self.container_storage_root / "worktrees" / "ia_mcp_vps" / "feat_test"
        with self.assertRaises(DeploymentBlockedError):
            unconfigured_runner.run_job(
                job_id="job_015",
                work_item_id="feat_test",
                repository="ia_mcp_vps",
                worktree_path=wt,
                prompt="Fails closed",
            )

    def test_16_legacy_jobs_remain_compatible(self) -> None:
        """16. Legacy jobs remain compatible."""
        class MockMCP:
            def __init__(self) -> None:
                self.tools: dict[str, Any] = {}

            def tool(self) -> Any:
                def decorator(fn: Any) -> Any:
                    self.tools[fn.__name__] = fn
                    return fn
                return decorator

        class MockAppConfig:
            raw = {
                "orchestrator": {
                    "storage_root": str(self.container_storage_root),
                    "runner_enabled": False,
                }
            }

        mcp = MockMCP()
        register_coding_job_tools(mcp, MockAppConfig())

        self.assertIn("coding_job_create", mcp.tools)
        self.assertIn("coding_job_status", mcp.tools)
        self.assertIn("coding_job_wait", mcp.tools)
        self.assertIn("coding_job_result", mcp.tools)
        self.assertIn("coding_job_changes", mcp.tools)
        self.assertIn("coding_job_artifact", mcp.tools)
        self.assertIn("coding_job_request_revision", mcp.tools)
        self.assertIn("coding_job_approve_changes", mcp.tools)
        self.assertIn("coding_job_publish_branch", mcp.tools)
        self.assertIn("coding_job_create_pull_request", mcp.tools)
        self.assertIn("coding_job_cancel", mcp.tools)
        self.assertIn("coding_job_cleanup", mcp.tools)

    def test_17_active_config_yaml_remains_untouched(self) -> None:
        """17. Active config.yaml remains untouched."""
        repo_root = Path(__file__).resolve().parent.parent
        active_config = repo_root / "config.yaml"
        self.assertFalse(active_config.exists())

    def test_18_deployed_agy_home_volume_default_is_agy_worker_agy_home(self) -> None:
        """18. Deployed Agy home volume default is agy-worker_agy_home and arbitrary names rejected."""
        self.assertEqual(DEFAULT_AGY_HOME_VOLUME, "agy-worker_agy_home")
        wt = self.container_storage_root / "worktrees" / "ia_mcp_vps" / "feat_test"
        self.runner.run_job(
            job_id="job_018",
            work_item_id="feat_test",
            repository="ia_mcp_vps",
            worktree_path=wt,
            prompt="Volume check",
        )
        created = self.mock_transport.created_containers[0]
        binds = created["payload"]["HostConfig"]["Binds"]
        self.assertIn("agy-worker_agy_home:/home/agy:rw", binds)

        # Reject arbitrary volume names
        bad_config = DockerRunnerConfig(
            enabled=True,
            container_storage_root=self.container_storage_root,
            host_storage_root=self.host_storage_root,
            agy_home_volume="arbitrary_volume_name",
        )
        with self.assertRaises(SecurityError):
            DockerAgyJobRunner(config=bad_config, client=self.mock_transport)

    def test_19_network_mode_is_exact_allowlisted_dedicated_network(self) -> None:
        """19. NetworkMode is the exact allowlisted dedicated network; none, host, container:*, arbitrary rejected."""
        self.assertEqual(DEFAULT_JOB_NETWORK, "agy-worker_default")
        wt = self.container_storage_root / "worktrees" / "ia_mcp_vps" / "feat_test"
        self.runner.run_job(
            job_id="job_019",
            work_item_id="feat_test",
            repository="ia_mcp_vps",
            worktree_path=wt,
            prompt="Network check",
        )
        created = self.mock_transport.created_containers[0]
        net_mode = created["payload"]["HostConfig"]["NetworkMode"]
        self.assertEqual(net_mode, "agy-worker_default")

        # Reject none, host, container:*, bridge, or arbitrary
        forbidden_networks = ["none", "host", "container:some_id", "bridge", "custom_net"]
        for net in forbidden_networks:
            bad_config = DockerRunnerConfig(
                enabled=True,
                container_storage_root=self.container_storage_root,
                host_storage_root=self.host_storage_root,
                job_network=net,
            )
            with self.assertRaises(SecurityError):
                DockerAgyJobRunner(config=bad_config, client=self.mock_transport)

    def test_20_docker_compose_contains_required_mcp_storage_bind(self) -> None:
        """20. docker-compose.yml contains the required MCP storage bind."""
        compose_path = Path(__file__).resolve().parent.parent / "docker-compose.yml"
        self.assertTrue(compose_path.exists())
        content = compose_path.read_text(encoding="utf-8")
        expected_bind = "/home/ubuntu/.local/share/ia-mcp-vps/coding-jobs:/var/lib/coding-jobs"
        self.assertIn(expected_bind, content)


if __name__ == "__main__":
    unittest.main()
