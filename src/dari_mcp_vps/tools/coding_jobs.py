from __future__ import annotations

import base64
import json
import re
import socket
import uuid
from typing import Any

DOCKER_SOCK = "/var/run/docker.sock"
WORKER_CONTAINER = "agy-worker"
JOB_ID_RE = re.compile(r"^job_[0-9a-f]{32}$")


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
    exec_id = created["Id"]
    output = _docker_request(
        "POST",
        f"/exec/{exec_id}/start",
        {"Detach": detach, "Tty": False},
    )
    return "" if detach else _demux(output)


def _validate_job_id(job_id: str) -> str:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ValueError("Invalid job_id")
    return job_id


def _read_json(job_id: str, filename: str) -> dict[str, Any]:
    _validate_job_id(job_id)
    output = _exec(["cat", f"/var/lib/coding-jobs/{job_id}/{filename}"], detach=False)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid worker response for {job_id}") from exc


def _validate_text_list(name: str, values: list[str] | None, *, maximum: int = 10) -> list[str]:
    clean = values or []
    if len(clean) > maximum:
        raise ValueError(f"{name} accepts at most {maximum} items")
    for value in clean:
        if not isinstance(value, str) or not value.strip() or len(value) > 1000:
            raise ValueError(f"Invalid {name} item")
    return [value.strip() for value in clean]


def register_coding_job_tools(mcp: Any, app_config: Any) -> None:
    @mcp.tool()
    def coding_job_create(
        repository: str,
        task_type: str,
        goal: str,
        acceptance_criteria: list[str],
        constraints: list[str] | None = None,
        base_branch: str = "main",
    ) -> dict[str, Any]:
        """Create a bounded asynchronous read-only Agy audit job."""
        if repository != "ia_mcp_vps":
            raise ValueError("Only repository alias ia_mcp_vps is allowed")
        if task_type != "audit":
            raise ValueError("Only task_type audit is allowed")
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
        _exec(["/opt/agy-job/run-job.sh", job_id, encoded], detach=True)
        return {"job_id": job_id, "status": "CREATED", "repository": repository}

    @mcp.tool()
    def coding_job_status(job_id: str) -> dict[str, Any]:
        """Return the current state and timestamps for an Agy coding job."""
        return _read_json(job_id, "job.json")

    @mcp.tool()
    def coding_job_result(job_id: str) -> dict[str, Any]:
        """Return the bounded structured result for a terminal Agy coding job."""
        status = _read_json(job_id, "job.json")
        if status.get("status") not in {"NOTION_REVIEW", "FAILED", "CANCELLED", "EXPIRED"}:
            return {"job_id": job_id, "status": status.get("status"), "result": None}
        if status.get("status") != "NOTION_REVIEW":
            return {"job_id": job_id, "status": status.get("status"), "error": status.get("error")}
        return _read_json(job_id, "result.json")
