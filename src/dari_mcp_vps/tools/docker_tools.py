from __future__ import annotations

import json
import socket
import urllib.parse
from typing import Any

from dari_mcp_vps.security import assert_allowed_name

DOCKER_SOCK = "/var/run/docker.sock"


def _docker_request(method: str, path: str, body: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
    """Minimal Docker Engine HTTP client over unix socket; avoids depending on docker CLI."""
    body = body or b""
    request = (
        f"{method} {path} HTTP/1.1\r\n"
        "Host: docker\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("utf-8") + body

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(30)
        sock.connect(DOCKER_SOCK)
        sock.sendall(request)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)

    raw = b"".join(chunks)
    header_bytes, _, response_body = raw.partition(b"\r\n\r\n")
    header_text = header_bytes.decode("iso-8859-1", errors="replace")
    lines = header_text.split("\r\n")
    status = int(lines[0].split()[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.lower()] = value.strip()

    if headers.get("transfer-encoding", "").lower() == "chunked":
        response_body = _decode_chunked(response_body)

    return status, headers, response_body


def _decode_chunked(data: bytes) -> bytes:
    out = bytearray()
    idx = 0
    while idx < len(data):
        end = data.find(b"\r\n", idx)
        if end == -1:
            break
        size_line = data[idx:end].split(b";", 1)[0]
        size = int(size_line, 16)
        idx = end + 2
        if size == 0:
            break
        out.extend(data[idx : idx + size])
        idx += size + 2
    return bytes(out)


def _json(method: str, path: str) -> Any:
    status, _headers, body = _docker_request(method, path)
    if status >= 400:
        raise RuntimeError(body.decode("utf-8", errors="replace"))
    if not body:
        return None
    return json.loads(body.decode("utf-8", errors="replace"))


def _container_name(container: dict[str, Any]) -> str:
    names = container.get("Names") or []
    if names:
        return str(names[0]).lstrip("/")
    return container.get("Id", "")[:12]


def _find_container(name: str) -> dict[str, Any]:
    containers = _json("GET", "/containers/json?all=1")
    for container in containers:
        names = [n.lstrip("/") for n in container.get("Names", [])]
        if name in names or name == container.get("Id"):
            return container
    raise RuntimeError(f"Container not found: {name}")


def _demux_docker_logs(raw: bytes) -> str:
    """Decode Docker raw log stream. Handles multiplexed and plain outputs."""
    if not raw:
        return ""
    idx = 0
    out = bytearray()
    multiplexed = False
    while idx + 8 <= len(raw):
        stream_type = raw[idx]
        size = int.from_bytes(raw[idx + 4 : idx + 8], "big")
        if stream_type not in (1, 2) or size < 0 or idx + 8 + size > len(raw):
            break
        multiplexed = True
        out.extend(raw[idx + 8 : idx + 8 + size])
        idx += 8 + size
    if multiplexed:
        return out.decode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace")


def register_docker_tools(mcp: Any, app_config: Any) -> None:
    @mcp.tool()
    def docker_ps() -> list[dict[str, Any]]:
        """List Docker containers via Docker socket; no docker CLI required."""
        containers = _json("GET", "/containers/json?all=1")
        return [
            {
                "name": _container_name(c),
                "id": c.get("Id", "")[:12],
                "image": c.get("Image"),
                "state": c.get("State"),
                "status": c.get("Status"),
                "ports": c.get("Ports", []),
            }
            for c in containers
        ]

    @mcp.tool()
    def container_inspect(container: str) -> dict[str, Any]:
        """Inspect an allowed container and return safe operational metadata."""
        assert_allowed_name(app_config.raw, "allowed_containers", container)
        target = _find_container(container)
        cid = target.get("Id")
        data = _json("GET", f"/containers/{cid}/json")
        state = data.get("State", {})
        config = data.get("Config", {})
        host_config = data.get("HostConfig", {})
        network_settings = data.get("NetworkSettings", {})
        return {
            "name": data.get("Name", "").lstrip("/"),
            "id": data.get("Id", "")[:12],
            "image": config.get("Image"),
            "created": data.get("Created"),
            "state": {
                "status": state.get("Status"),
                "running": state.get("Running"),
                "paused": state.get("Paused"),
                "restarting": state.get("Restarting"),
                "oom_killed": state.get("OOMKilled"),
                "dead": state.get("Dead"),
                "exit_code": state.get("ExitCode"),
                "started_at": state.get("StartedAt"),
                "finished_at": state.get("FinishedAt"),
                "health": state.get("Health", {}).get("Status") if state.get("Health") else None,
            },
            "restart_count": data.get("RestartCount"),
            "ports": network_settings.get("Ports"),
            "mounts": [
                {
                    "type": m.get("Type"),
                    "name": m.get("Name"),
                    "source": m.get("Source"),
                    "destination": m.get("Destination"),
                    "mode": m.get("Mode"),
                    "rw": m.get("RW"),
                }
                for m in data.get("Mounts", [])
            ],
            "restart_policy": host_config.get("RestartPolicy"),
        }

    @mcp.tool()
    def docker_logs(container: str, lines: int = 100) -> str:
        """Return recent logs for an allowed Docker container."""
        assert_allowed_name(app_config.raw, "allowed_containers", container)
        target = _find_container(container)
        cid = target.get("Id")
        tail = min(max(1, int(lines)), app_config.max_log_lines)
        path = f"/containers/{cid}/logs?stdout=1&stderr=1&timestamps=1&tail={tail}"
        status, _headers, body = _docker_request("GET", path)
        if status >= 400:
            raise RuntimeError(body.decode("utf-8", errors="replace"))
        return _demux_docker_logs(body)

    @mcp.tool()
    def docker_logs_filtered(
        container: str,
        lines: int = 200,
        grep: str | None = None,
        case_sensitive: bool = False,
        since: str | None = None,
    ) -> str:
        """Return recent allowed-container logs with optional grep and since timestamp/seconds."""
        assert_allowed_name(app_config.raw, "allowed_containers", container)
        target = _find_container(container)
        cid = target.get("Id")
        tail = min(max(1, int(lines)), app_config.max_log_lines)
        params = {"stdout": "1", "stderr": "1", "timestamps": "1", "tail": str(tail)}
        if since:
            params["since"] = since
        query = urllib.parse.urlencode(params)
        status, _headers, body = _docker_request("GET", f"/containers/{cid}/logs?{query}")
        if status >= 400:
            raise RuntimeError(body.decode("utf-8", errors="replace"))
        text = _demux_docker_logs(body)
        if grep:
            needle = grep if case_sensitive else grep.lower()
            lines_out = []
            for line in text.splitlines():
                hay = line if case_sensitive else line.lower()
                if needle in hay:
                    lines_out.append(line)
            text = "\n".join(lines_out)
        return text[:20000]

    @mcp.tool()
    def docker_restart(container: str) -> str:
        """Restart an allowed Docker container."""
        assert_allowed_name(app_config.raw, "allowed_containers", container)
        target = _find_container(container)
        cid = target.get("Id")
        status, _headers, body = _docker_request("POST", f"/containers/{cid}/restart?t=10")
        if status >= 400:
            raise RuntimeError(body.decode("utf-8", errors="replace"))
        return f"restarted {container}"
