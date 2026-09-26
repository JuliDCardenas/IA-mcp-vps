from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dari_mcp_vps.worktree_manager import SecurityError, WorktreeManagerError

ALLOWLISTED_AGY_IMAGES = {
    "ia-mcp-vps-agy-worker:local",
    "ia-mcp-vps-agy-worker:latest",
}
DEFAULT_AGY_IMAGE = "ia-mcp-vps-agy-worker:local"

ALLOWLISTED_AGY_VOLUMES = {
    "agy-worker_agy_home",
    "agy_home",
}
DEFAULT_AGY_HOME_VOLUME = "agy-worker_agy_home"

ALLOWLISTED_JOB_NETWORKS = {
    "agy-worker_default",
}
DEFAULT_JOB_NETWORK = "agy-worker_default"

MAX_LOG_BYTES = 1_000_000


class DockerRunnerError(WorktreeManagerError):
    """Base exception for Docker container runner errors."""


class DeploymentBlockedError(DockerRunnerError):
    """Raised when execution cannot proceed because per-job container runner is not deployed or configured."""


@dataclass(frozen=True)
class DockerRunnerConfig:
    enabled: bool = False
    container_storage_root: Path = Path("/var/lib/coding-jobs")
    host_storage_root: Path | None = None
    agy_image: str = DEFAULT_AGY_IMAGE
    agy_home_volume: str = DEFAULT_AGY_HOME_VOLUME
    job_network: str = DEFAULT_JOB_NETWORK
    timeout_seconds: int = 1200
    pids_limit: int = 256
    memory_limit: int = 2147483648  # 2 GiB
    nano_cpus: int = 2000000000  # 2.0 CPUs
    socket_path: str = "/var/run/docker.sock"

    @property
    def is_configured(self) -> bool:
        return (
            self.enabled
            and self.host_storage_root is not None
            and bool(self.job_network)
        )


def _decode_chunked(data: bytes) -> bytes:
    out = bytearray()
    idx = 0
    while idx < len(data):
        end = data.find(b"\r\n", idx)
        if end == -1:
            break
        size = int(data[idx:end].split(b";", 1)[0], 16)
        idx = end + 2
        if size == 0:
            break
        out.extend(data[idx : idx + size])
        idx += size + 2
    return bytes(out)


def _demux_docker_logs(raw: bytes) -> str:
    if not raw:
        return ""
    idx = 0
    out = bytearray()
    while idx + 8 <= len(raw):
        stream = raw[idx]
        size = int.from_bytes(raw[idx + 4 : idx + 8], "big")
        if stream not in (1, 2) or idx + 8 + size > len(raw):
            return raw.decode("utf-8", errors="replace")
        out.extend(raw[idx + 8 : idx + 8 + size])
        idx += 8 + size
    return out.decode("utf-8", errors="replace")


def _redact_sensitive(text: str) -> str:
    patterns = [
        r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+",
        r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)['\"]?[A-Za-z0-9._~+/=-]+['\"]?",
        r"github_pat_[A-Za-z0-9_]{30,}",
        r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}",
        r"AIza[0-9A-Za-z_-]{20,}",
    ]
    for pattern in patterns:
        text = re.sub(pattern, "[REDACTED]", text)
    return text


def resolve_worktree_bind_source(
    container_worktree: Path,
    container_storage_root: Path,
    host_storage_root: Path,
) -> Path:
    """Resolve and validate the exact host path to bind mount for a job worktree.

    Guarantees:
    - Path must reside strictly inside container_storage_root / 'worktrees'
    - No symlinks are permitted anywhere along the path
    - Path translation matches strictly relative parts
    - Bases, other jobs, or parent directories can NEVER be mounted
    """
    cont_wt = container_worktree.resolve()
    cont_root = container_storage_root.resolve()
    host_root = host_storage_root.resolve()

    try:
        rel = cont_wt.relative_to(cont_root)
    except ValueError as exc:
        raise SecurityError(f"Worktree path escapes container storage root: {cont_wt}") from exc

    if not rel.parts or rel.parts[0] != "worktrees" or len(rel.parts) < 3:
        raise SecurityError(f"Worktree path must be a specific work item under 'worktrees/<alias>/<work_item>', got: {rel}")

    # Forbid base repositories or other internal directories
    if "bases" in rel.parts or "jobs" in rel.parts:
        raise SecurityError(f"Attempted to access forbidden directory in path: {rel}")

    # Check for symlinks on the container side
    curr = cont_wt
    while curr != cont_root and curr != curr.parent:
        if os.path.islink(curr):
            raise SecurityError(f"Symlinks are forbidden in worktree path: {curr}")
        curr = curr.parent

    # Construct host path
    host_path = (host_root / rel).resolve()
    try:
        host_rel = host_path.relative_to(host_root)
    except ValueError as exc:
        raise SecurityError(f"Host path escapes host storage root: {host_path}") from exc

    if host_rel != rel:
        raise SecurityError(f"Path translation discrepancy: {host_rel} != {rel}")

    return host_path


class DockerClient:
    """Minimal UNIX socket client for Docker Engine REST API."""

    def __init__(self, socket_path: str = "/var/run/docker.sock") -> None:
        self.socket_path = socket_path

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> bytes:
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        headers = [
            f"{method} {path} HTTP/1.1",
            "Host: docker",
            f"Content-Length: {len(body)}",
            "Connection: close",
        ]
        if payload is not None:
            headers.append("Content-Type: application/json")
        request_bytes = ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8") + body

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(self.socket_path)
            sock.sendall(request_bytes)
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)

        raw = b"".join(chunks)
        header_bytes, _, response = raw.partition(b"\r\n\r\n")
        lines = header_bytes.decode("iso-8859-1", errors="replace").split("\r\n")
        status_line = lines[0].split()
        status = int(status_line[1]) if len(status_line) > 1 else 500

        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                if k.strip().lower() == "transfer-encoding" and "chunked" in v.lower():
                    response = _decode_chunked(response)
                    break

        if status >= 400:
            raise DockerRunnerError(
                f"Docker API error ({status}): {response.decode('utf-8', errors='replace')[:1000]}"
            )
        return response

    def json_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> Any:
        resp = self.request(method, path, payload, timeout=timeout)
        return json.loads(resp.decode("utf-8")) if resp else None


class DockerAgyJobRunner:
    """Orchestrates ephemeral per-job Agy containers with strict security isolation."""

    def __init__(
        self,
        config: DockerRunnerConfig | None = None,
        client: Any = None,
        runner_configured: bool | None = None,
        executor_fn: Any = None,
        host_storage_root: Path | None = None,
    ) -> None:
        if config is None:
            enabled = bool(runner_configured) if runner_configured is not None else False
            config = DockerRunnerConfig(
                enabled=enabled,
                host_storage_root=host_storage_root,
            )
        elif runner_configured is not None:
            config = DockerRunnerConfig(
                enabled=runner_configured,
                container_storage_root=config.container_storage_root,
                host_storage_root=host_storage_root or config.host_storage_root,
                agy_image=config.agy_image,
                agy_home_volume=config.agy_home_volume,
                job_network=config.job_network,
                timeout_seconds=config.timeout_seconds,
                pids_limit=config.pids_limit,
                memory_limit=config.memory_limit,
                nano_cpus=config.nano_cpus,
                socket_path=config.socket_path,
            )
        self.config = config
        self.client = client or DockerClient(socket_path=self.config.socket_path)
        self._executor_fn = executor_fn

        # Strict allowlist validations
        if self.config.agy_image not in ALLOWLISTED_AGY_IMAGES:
            raise SecurityError(
                f"Configured Agy image is not allowlisted: {self.config.agy_image!r} "
                f"(allowed: {sorted(ALLOWLISTED_AGY_IMAGES)})"
            )

        if self.config.agy_home_volume not in ALLOWLISTED_AGY_VOLUMES:
            raise SecurityError(
                f"Configured Agy home volume is not allowlisted: {self.config.agy_home_volume!r} "
                f"(allowed: {sorted(ALLOWLISTED_AGY_VOLUMES)})"
            )

        if self.config.job_network not in ALLOWLISTED_JOB_NETWORKS:
            raise SecurityError(
                f"Configured job network is not allowlisted: {self.config.job_network!r} "
                f"(allowed: {sorted(ALLOWLISTED_JOB_NETWORKS)})"
            )

    @property
    def is_configured(self) -> bool:
        if self._executor_fn is not None:
            return self.config.enabled
        return self.config.is_configured

    @property
    def is_isolated_environment_configured(self) -> bool:
        """Alias for compatibility with earlier checks."""
        return self.is_configured

    def build_command(
        self,
        job_or_conv: Any,
        prompt: str,
        is_revision: bool = False,
    ) -> list[str]:
        """Construct fixed Agy 1.2.11 CLI arguments."""
        if hasattr(job_or_conv, "conversation_id"):
            conv_id = getattr(job_or_conv, "conversation_id")
        else:
            conv_id = str(job_or_conv) if job_or_conv else None

        if is_revision and conv_id:
            return [
                "agy",
                "--conversation",
                conv_id,
                "-p",
                prompt,
                "--mode=accept-edits",
                "--sandbox",
                "--print-timeout",
                "20m",
                "--output-format",
                "json",
            ]
        return [
            "agy",
            "-p",
            prompt,
            "--mode=accept-edits",
            "--sandbox",
            "--print-timeout",
            "20m",
            "--output-format",
            "json",
        ]

    def build_environment(self, worktree_path: Path | None = None) -> dict[str, str]:
        """Return the minimal scrubbed environment dictionary for Agy."""
        return {
            "PATH": "/home/agy/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": "/home/agy",
            "TMPDIR": "/tmp",
            "LANG": "C.UTF-8",
        }

    def run_job(
        self,
        job_id: str,
        work_item_id: str,
        repository: str,
        worktree_path: Path,
        prompt: str,
        is_revision: bool = False,
        conversation_id: str | None = None,
    ) -> tuple[int, str, str | None]:
        """Create, start, wait, collect logs, and cleanup an ephemeral job container.

        Returns (exit_code, sanitized_output, real_conversation_id).
        """
        if not self.is_configured:
            raise DeploymentBlockedError(
                "BLOCKED_DEPLOYMENT: Per-job isolated Docker container runner is not configured. "
                "Requires orchestrator.host_storage_root and runner_enabled: true in configuration."
            )

        if self._executor_fn:
            cmd = self.build_command(conversation_id, prompt, is_revision=is_revision)
            env = self.build_environment(worktree_path)
            return self._executor_fn(job_id, cmd, env, worktree_path)

        assert self.config.host_storage_root is not None

        # 1. Resolve and validate the exact host bind mount
        host_worktree = resolve_worktree_bind_source(
            container_worktree=worktree_path,
            container_storage_root=self.config.container_storage_root,
            host_storage_root=self.config.host_storage_root,
        )

        # 2. Server-generated deterministic container name and labels
        container_name = f"agy-job-{job_id}"
        labels = {
            "ia_mcp_vps.component": "agy-job",
            "ia_mcp_vps.job_id": job_id,
            "ia_mcp_vps.work_item_id": work_item_id,
            "ia_mcp_vps.repository": repository,
        }

        # 3. Fixed command array & minimal environment
        cmd = self.build_command(conversation_id, prompt, is_revision=is_revision)
        env_list = [
            "HOME=/home/agy",
            "PATH=/home/agy/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG=C.UTF-8",
        ]

        # 4. Strict HostConfig
        host_config = {
            "Binds": [
                f"{host_worktree}:/workspace:rw",
                f"{self.config.agy_home_volume}:/home/agy:rw",
            ],
            "Tmpfs": {
                "/tmp": "rw,noexec,nosuid,size=256m",
            },
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "NetworkMode": self.config.job_network,
            "PortBindings": {},
            "PublishAllPorts": False,
            "PidsLimit": self.config.pids_limit,
            "Memory": self.config.memory_limit,
            "NanoCpus": self.config.nano_cpus,
            "RestartPolicy": {"Name": "no"},
            "AutoRemove": False,
        }

        create_payload = {
            "Image": self.config.agy_image,
            "User": "10001:10001",
            "WorkingDir": "/workspace",
            "Cmd": cmd,
            "Env": env_list,
            "Labels": labels,
            "HostConfig": host_config,
        }

        # 5. Container execution lifecycle
        cid: str | None = None
        try:
            # Create
            created = self.client.json_request("POST", f"/containers/create?name={container_name}", create_payload)
            cid = created["Id"]

            # Start
            self.client.request("POST", f"/containers/{cid}/start")

            # Wait with bounded timeout
            wait_timeout = self.config.timeout_seconds + 10
            wait_res = self.client.json_request(
                "POST",
                f"/containers/{cid}/wait?condition=not-running",
                timeout=wait_timeout,
            )
            exit_code = int(wait_res.get("StatusCode", 1))

            # Collect logs
            raw_logs = self.client.request("GET", f"/containers/{cid}/logs?stdout=1&stderr=1")
            demuxed = _demux_docker_logs(raw_logs)
            if len(demuxed) > MAX_LOG_BYTES:
                demuxed = demuxed[-MAX_LOG_BYTES:]
            sanitized_logs = _redact_sensitive(demuxed)

            # Extract real conversation_id if emitted in json
            real_conv_id: str | None = None
            for line in reversed(sanitized_logs.splitlines()):
                line_str = line.strip()
                if line_str.startswith("{") and line_str.endswith("}"):
                    try:
                        parsed = json.loads(line_str)
                        if "conversation_id" in parsed:
                            real_conv_id = str(parsed["conversation_id"])
                            break
                    except Exception:
                        pass

            return exit_code, sanitized_logs, real_conv_id

        except socket.timeout:
            if cid:
                self._stop_and_remove(cid)
                cid = None
            raise TimeoutError(f"Agy job execution timed out after {self.config.timeout_seconds} seconds") from None

        finally:
            if cid:
                self._stop_and_remove(cid)

    def execute(
        self,
        job: Any,
        prompt: str,
        is_revision: bool = False,
        timeout_seconds: int = 1200,
    ) -> tuple[int, str, str | None]:
        """Convenience method matching AgyExecutionAdapter signature."""
        if not self.is_configured:
            raise DeploymentBlockedError(
                "BLOCKED_DEPLOYMENT: Per-job isolated Docker container runner is not configured. "
                "Requires orchestrator.host_storage_root and runner_enabled: true in configuration."
            )
        if self._executor_fn:
            cmd = self.build_command(job, prompt, is_revision=is_revision)
            env = self.build_environment(Path(job.worktree_path))
            return self._executor_fn(job, cmd, env, Path(job.worktree_path))
        return self.run_job(
            job_id=job.job_id,
            work_item_id=job.work_item_id,
            repository=job.repository,
            worktree_path=Path(job.worktree_path),
            prompt=prompt,
            is_revision=is_revision,
            conversation_id=job.conversation_id,
        )

    def cancel(self, job_id: str) -> bool:
        """Convenience alias for cancel_job."""
        return self.cancel_job(job_id)

    def _stop_and_remove(self, cid: str) -> None:
        try:
            self.client.request("POST", f"/containers/{cid}/stop?t=2", timeout=10)
        except Exception:
            pass
        try:
            self.client.request("DELETE", f"/containers/{cid}?v=1&force=1", timeout=10)
        except Exception:
            pass

    def cancel_job(self, job_id: str) -> bool:
        """Cancel container execution strictly matching the validated job_id."""
        filter_json = json.dumps({"label": [f"ia_mcp_vps.job_id={job_id}"]})
        try:
            containers = self.client.json_request("GET", f"/containers/json?all=1&filters={filter_json}")
        except Exception:
            return False

        cancelled = False
        for c in (containers or []):
            labels = c.get("Labels", {})
            if labels.get("ia_mcp_vps.job_id") == job_id:
                cid = c["Id"]
                self._stop_and_remove(cid)
                cancelled = True
        return cancelled


AgyExecutionAdapter = DockerAgyJobRunner
