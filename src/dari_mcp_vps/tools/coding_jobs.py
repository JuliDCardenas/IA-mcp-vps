from __future__ import annotations

import base64
import json
import os
import re
import socket
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dari_mcp_vps.docker_runner import (
    DEFAULT_AGY_HOME_VOLUME,
    DEFAULT_AGY_IMAGE,
    DEFAULT_JOB_NETWORK,
    DockerAgyJobRunner,
    DockerRunnerConfig,
)
from dari_mcp_vps.persistent_job import PersistentJobManager
from dari_mcp_vps.promoter import BranchPromoter, GitHubPRClient
from dari_mcp_vps.worktree_manager import WorktreeManager

DOCKER_SOCK = "/var/run/docker.sock"
WORKER_CONTAINER = "agy-worker"
JOB_ID_RE = re.compile(r"^job_[0-9a-f]{32}$")
ARTIFACT_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
TERMINAL_STATES = {"NOTION_REVIEW", "FAILED", "CANCELLED", "EXPIRED"}
TASK_SCRIPTS = {
    "audit": "/opt/agy-job/run-job.sh",
    "implement": "/opt/agy-job/run-implementation-job.sh",
}


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


def _docker_request(method: str, path: str, payload: dict[str, Any] | None = None) -> bytes:
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    headers = [
        f"{method} {path} HTTP/1.1",
        "Host: docker",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    if payload is not None:
        headers.append("Content-Type: application/json")
    request = ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8") + body

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(30)
        sock.connect(DOCKER_SOCK)
        sock.sendall(request)
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)

    raw = b"".join(chunks)
    header_bytes, _, response = raw.partition(b"\r\n\r\n")
    lines = header_bytes.decode("iso-8859-1", errors="replace").split("\r\n")
    status = int(lines[0].split()[1])
    response_headers = {
        key.lower(): value.strip()
        for line in lines[1:]
        if ":" in line
        for key, value in [line.split(":", 1)]
    }
    if response_headers.get("transfer-encoding", "").lower() == "chunked":
        response = _decode_chunked(response)
    if status >= 400:
        raise RuntimeError(response.decode("utf-8", errors="replace")[:2000])
    return response


def _json_request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    body = _docker_request(method, path, payload)
    return json.loads(body.decode("utf-8")) if body else None


def _worker_id() -> str:
    containers = _json_request("GET", "/containers/json?all=1")
    for container in containers:
        names = [name.lstrip("/") for name in container.get("Names", [])]
        if WORKER_CONTAINER in names:
            if container.get("State") != "running":
                raise RuntimeError("Agy worker is not running")
            return str(container["Id"])
    raise RuntimeError("Agy worker container not found")


def _demux(raw: bytes) -> str:
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


def _exec(cmd: list[str], *, detach: bool) -> str:
    cid = _worker_id()
    created = _json_request(
        "POST",
        f"/containers/{cid}/exec",
        {
            "AttachStdout": not detach,
            "AttachStderr": not detach,
            "Tty": False,
            "User": "10001:10001",
            "WorkingDir": "/workspace/IA-mcp-vps",
            "Cmd": cmd,
        },
    )
    output = _docker_request(
        "POST",
        f"/exec/{created['Id']}/start",
        {"Detach": detach, "Tty": False},
    )
    return "" if detach else _demux(output)


def _validate_job_id(job_id: str) -> str:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ValueError("Invalid job_id")
    return job_id


def _validate_artifact_path(path: str) -> str:
    if (
        not path
        or len(path) > 240
        or path.startswith("/")
        or ".." in path.split("/")
        or path == ".git"
        or path.startswith(".git/")
        or not ARTIFACT_PATH_RE.fullmatch(path)
    ):
        raise ValueError("Invalid artifact path")
    return path


def _read_text(job_id: str, filename: str) -> str:
    _validate_job_id(job_id)
    return _exec(["cat", f"/var/lib/coding-jobs/{job_id}/{filename}"], detach=False)


def _read_json(job_id: str, filename: str) -> dict[str, Any]:
    output = _read_text(job_id, filename)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid worker response for {job_id}") from exc


def _redact(text: str) -> str:
    patterns = [
        r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+",
        r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)\S+",
        r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}",
        r"AIza[0-9A-Za-z_-]{20,}",
    ]
    for pattern in patterns:
        text = re.sub(pattern, r"\1[REDACTED]" if pattern.startswith("(?i)(") else "[REDACTED]", text)
    return text


def _validate_text_list(name: str, values: list[str] | None, *, maximum: int = 10) -> list[str]:
    clean = values or []
    if len(clean) > maximum:
        raise ValueError(f"{name} accepts at most {maximum} items")
    for value in clean:
        if not isinstance(value, str) or not value.strip() or len(value) > 1000:
            raise ValueError(f"Invalid {name} item")
    return [value.strip() for value in clean]


def register_coding_job_tools(mcp: Any, app_config: Any) -> None:
    def _storage_root() -> Path:
        storage_root_str = (
            getattr(app_config, "raw", {}).get("orchestrator", {}).get("storage_root")
            or os.getenv("IA_MCP_VPS_STORAGE_ROOT")
            or "/var/lib/coding-jobs"
        )
        return Path(storage_root_str)

    _mgr_instance: PersistentJobManager | None = None

    def _get_mgr() -> PersistentJobManager:
        nonlocal _mgr_instance
        if _mgr_instance is None:
            root = _storage_root()
            worktree_mgr = WorktreeManager(storage_root=root)
            orchestrator_cfg = getattr(app_config, "raw", {}).get("orchestrator", {})
            runner_enabled = bool(orchestrator_cfg.get("runner_enabled", False))
            if not runner_enabled and os.getenv("IA_CODING_JOB_RUNNER_ENABLED", "0").lower() in ("1", "true", "yes"):
                runner_enabled = True

            host_storage_root_str = orchestrator_cfg.get("host_storage_root") or os.getenv("IA_MCP_VPS_HOST_STORAGE_ROOT")
            host_storage_root = Path(host_storage_root_str) if host_storage_root_str else None

            runner_cfg = DockerRunnerConfig(
                enabled=runner_enabled,
                container_storage_root=root,
                host_storage_root=host_storage_root,
                agy_image=orchestrator_cfg.get("agy_image", DEFAULT_AGY_IMAGE),
                agy_home_volume=orchestrator_cfg.get("agy_home_volume", DEFAULT_AGY_HOME_VOLUME),
                job_network=orchestrator_cfg.get("job_network", DEFAULT_JOB_NETWORK),
                timeout_seconds=int(orchestrator_cfg.get("timeout_seconds", 1200)),
            )
            runner = DockerAgyJobRunner(config=runner_cfg)
            _mgr_instance = PersistentJobManager(
                storage_root=root,
                worktree_manager=worktree_mgr,
                execution_adapter=runner,
            )
        return _mgr_instance

    def is_persistent_job(job_id: str) -> bool:
        _validate_job_id(job_id)
        job_file = _storage_root() / "jobs" / f"{job_id}.json"
        try:
            return job_file.exists()
        except OSError:
            return False

    def status_payload(job_id: str) -> dict[str, Any]:
        if is_persistent_job(job_id):
            job = _get_mgr().get_job(job_id)
            return asdict(job)
        return _read_json(job_id, "job.json")

    @mcp.tool()
    def coding_job_create(
        repository: str,
        task_type: str,
        goal: str,
        acceptance_criteria: list[str],
        constraints: list[str] | None = None,
        base_branch: str = "main",
        work_item_id: str | None = None,
        execution_mode: str = "legacy",
    ) -> dict[str, Any]:
        """Create a bounded asynchronous Agy audit or isolated implementation job."""
        if execution_mode == "persistent" or work_item_id is not None:
            goal = goal.strip()
            if not goal or len(goal) > 4000:
                raise ValueError("goal must contain 1-4000 characters")
            criteria = _validate_text_list("acceptance_criteria", acceptance_criteria)
            if not criteria:
                raise ValueError("At least one acceptance criterion is required")
            constraints_clean = _validate_text_list("constraints", constraints)

            mgr = _get_mgr()
            job = mgr.create_job(
                repository=repository,
                goal=goal,
                acceptance_criteria=criteria,
                constraints=constraints_clean,
                base_branch=base_branch,
                work_item_id=work_item_id,
                task_type=task_type,
            )
            try:
                job = mgr.run_execution(job.job_id)
            except Exception:
                job = mgr.get_job(job.job_id)

            return {
                "job_id": job.job_id,
                "status": job.status,
                "repository": job.repository,
                "task_type": job.task_type,
                "work_item_id": job.work_item_id,
                "feature_branch": job.feature_branch,
                "execution_mode": "persistent",
                "error": job.error,
            }

        if repository != "ia_mcp_vps":
            raise ValueError("Only repository alias ia_mcp_vps is allowed")
        if task_type not in TASK_SCRIPTS:
            raise ValueError("task_type must be audit or implement")
        if base_branch != "main":
            raise ValueError("Only base_branch main is allowed")
        goal = goal.strip()
        if not goal or len(goal) > 4000:
            raise ValueError("goal must contain 1-4000 characters")
        criteria = _validate_text_list("acceptance_criteria", acceptance_criteria)
        if not criteria:
            raise ValueError("At least one acceptance criterion is required")
        constraints_clean = _validate_text_list("constraints", constraints)

        job_id = f"job_{uuid.uuid4().hex}"
        request = {
            "job_id": job_id,
            "repository": repository,
            "task_type": task_type,
            "goal": goal,
            "acceptance_criteria": criteria,
            "constraints": constraints_clean,
            "base_branch": base_branch,
        }
        encoded = base64.urlsafe_b64encode(json.dumps(request).encode("utf-8")).decode("ascii")
        _exec([TASK_SCRIPTS[task_type], job_id, encoded], detach=True)
        return {"job_id": job_id, "status": "CREATED", "repository": repository, "task_type": task_type}

    @mcp.tool()
    def coding_job_status(job_id: str) -> dict[str, Any]:
        """Return the current state and timestamps for an Agy coding job."""
        return status_payload(job_id)

    @mcp.tool()
    def coding_job_wait(job_id: str, timeout_seconds: int = 90, poll_seconds: int = 2) -> dict[str, Any]:
        """Wait a bounded time for a coding job to reach a terminal state."""
        timeout_seconds = min(max(int(timeout_seconds), 1), 120)
        poll_seconds = min(max(int(poll_seconds), 1), 10)
        deadline = time.monotonic() + timeout_seconds
        status = status_payload(job_id)
        while status.get("status") not in TERMINAL_STATES and time.monotonic() < deadline:
            time.sleep(poll_seconds)
            status = status_payload(job_id)
        return {**status, "wait_timed_out": status.get("status") not in TERMINAL_STATES}

    @mcp.tool()
    def coding_job_result(job_id: str) -> dict[str, Any]:
        """Return the bounded structured result or sanitized failure diagnostics."""
        if is_persistent_job(job_id):
            job = _get_mgr().get_job(job_id)
            return {
                "job_id": job.job_id,
                "status": job.status,
                "work_item_id": job.work_item_id,
                "feature_branch": job.feature_branch,
                "conversation_id": job.conversation_id,
                "revision_count": job.revision_count,
                "validation_report": job.validation_report,
                "publish_info": job.publish_info,
                "pr_info": job.pr_info,
                "error": job.error,
                "exit_code": job.exit_code,
                "execution_output_tail": job.execution_output_tail,
                "approved_commit_sha": job.approved_commit_sha,
            }

        status = status_payload(job_id)
        if status.get("status") not in TERMINAL_STATES:
            return {"job_id": job_id, "status": status.get("status"), "result": None}
        if status.get("status") == "NOTION_REVIEW":
            return _read_json(job_id, "result.json")

        diagnostic: dict[str, Any] = {}
        try:
            raw = _read_json(job_id, "raw.json")
            diagnostic = {
                "agy_status": raw.get("status"),
                "agy_error": _redact(str(raw.get("error") or ""))[:1000] or None,
                "conversation_id": raw.get("conversation_id"),
                "response_preview": _redact(str(raw.get("response") or ""))[:1000] or None,
            }
        except Exception:
            pass
        try:
            diagnostic["stderr_tail"] = _redact(_read_text(job_id, "stderr.log"))[-2000:] or None
        except Exception:
            diagnostic["stderr_tail"] = None
        return {
            "job_id": job_id,
            "status": status.get("status"),
            "error": status.get("error"),
            "diagnostic": diagnostic,
        }

    @mcp.tool()
    def coding_job_changes(job_id: str) -> dict[str, Any]:
        """Return the validated changed-file manifest for an implementation job."""
        if is_persistent_job(job_id):
            job = _get_mgr().get_job(job_id)
            if job.status not in {"NOTION_REVIEW", "CHANGES_APPROVED", "BRANCH_PUBLISHED", "PR_CREATED"}:
                raise ValueError("Implementation job is not ready for review")
            manifest_changes = job.validation_report.get("changes", []) if job.validation_report else []
            return {"job_id": job.job_id, "base_commit": job.base_commit, "changes": manifest_changes}

        status = status_payload(job_id)
        if status.get("status") != "NOTION_REVIEW" or status.get("task_type") != "implement":
            raise ValueError("Implementation job is not ready for review")
        manifest = _read_json(job_id, "manifest.json")
        return {"job_id": job_id, **manifest}

    @mcp.tool()
    def coding_job_artifact(job_id: str, path: str) -> dict[str, Any]:
        """Return one validated changed text file from an isolated implementation job."""
        clean_path = _validate_artifact_path(path)
        if is_persistent_job(job_id):
            job = _get_mgr().get_job(job_id)
            wt_dir = Path(job.worktree_path)
            target = (wt_dir / clean_path).resolve()
            try:
                target.relative_to(wt_dir)
            except ValueError:
                raise ValueError("Invalid artifact path")
            if not target.exists() or not target.is_file():
                raise ValueError(f"Artifact not found: {clean_path}")
            content = target.read_text(encoding="utf-8", errors="replace")
            return {"job_id": job_id, "path": clean_path, "content": content}

        status = status_payload(job_id)
        if status.get("status") != "NOTION_REVIEW" or status.get("task_type") != "implement":
            raise ValueError("Implementation job is not ready for review")
        encoded = base64.b64encode(clean_path.encode("utf-8")).decode("ascii")
        content = _exec(["/opt/agy-job/read-artifact.sh", job_id, encoded], detach=False)
        return {"job_id": job_id, "path": clean_path, "content": content}

    @mcp.tool()
    def coding_job_request_revision(job_id: str, feedback: str) -> dict[str, Any]:
        """Request an implementation revision with bounded feedback (max 3 cycles)."""
        mgr = _get_mgr()
        job = mgr.request_revision(job_id=job_id, feedback=feedback)
        try:
            job = mgr.run_execution(job_id=job_id, is_revision=True, feedback=feedback)
        except Exception:
            job = mgr.get_job(job_id)
        return asdict(job)

    @mcp.tool()
    def coding_job_approve_changes(
        job_id: str,
        expected_validation_hash: str,
        expected_base_commit: str,
    ) -> dict[str, Any]:
        """Approve validated worktree changes from NOTION_REVIEW state."""
        job = _get_mgr().approve_changes(
            job_id=job_id,
            expected_validation_hash=expected_validation_hash,
            expected_base_commit=expected_base_commit,
        )
        return asdict(job)

    @mcp.tool()
    def coding_job_publish_branch(job_id: str) -> dict[str, Any]:
        """Publish the approved feature branch via the isolated promoter boundary."""
        job = _get_mgr().publish_branch(job_id=job_id)
        return asdict(job)

    @mcp.tool()
    def coding_job_create_pull_request(
        job_id: str,
        title: str | None = None,
        body: str | None = None,
    ) -> dict[str, Any]:
        """Create a Pull Request for the published feature branch."""
        job = _get_mgr().create_pull_request(job_id=job_id, title=title, body=body)
        return asdict(job)

    @mcp.tool()
    def coding_job_cancel(job_id: str, reason: str | None = None) -> dict[str, Any]:
        """Cancel an active coding job idempotently without deleting the worktree."""
        if is_persistent_job(job_id):
            job = _get_mgr().cancel_job(job_id=job_id, reason=reason)
            return asdict(job)
        return {"job_id": job_id, "status": "CANCELLED", "reason": reason}

    @mcp.tool()
    def coding_job_cleanup(job_id: str, confirm_discard_unpublished: bool = False) -> dict[str, Any]:
        """Clean up the assigned persistent worktree safely, preserving base repository."""
        if is_persistent_job(job_id):
            job = _get_mgr().cleanup_job(
                job_id=job_id,
                confirm_discard_unpublished=confirm_discard_unpublished,
            )
            return asdict(job)
        return {"job_id": job_id, "status": "CLEANED_UP", "message": "Legacy job cleanup completed"}
