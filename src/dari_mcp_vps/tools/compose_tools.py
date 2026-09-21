from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dari_mcp_vps.security import SecurityError
from dari_mcp_vps.tools.docker_tools import _json, _docker_request, _demux_docker_logs


def _project_config(config: dict[str, Any], project: str) -> dict[str, Any]:
    projects = config.get("allowed_compose_projects", {})
    if project not in projects:
        raise SecurityError(f"Compose project not allowed: {project}")
    return projects[project]


def _compose_path(config: dict[str, Any], project: str) -> Path:
    value = _project_config(config, project).get("path")
    if not value:
        raise SecurityError(f"Compose project has no path: {project}")
    path = Path(value).resolve()
    if not path.exists():
        raise FileNotFoundError(str(path))
    return path


def _load_compose(compose_file: Path) -> dict[str, Any]:
    return yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}


def _truncate(text: str, limit: int = 20000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def _compose_project_names(project: str, cfg: dict[str, Any]) -> set[str]:
    names = {project}
    for key in ("compose_project_name", "name"):
        value = cfg.get(key)
        if value:
            names.add(str(value))
    # Common Compose normalization fallback: directory/repo style names often swap '-' and '_'.
    names.add(project.replace("_", "-"))
    names.add(project.replace("-", "_"))
    return names


def _containers_for_project(project: str, project_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    project_names = _compose_project_names(project, project_cfg)
    explicit_container_names = set(project_cfg.get("container_names", []))
    containers = _json("GET", "/containers/json?all=1")
    out = []
    for container in containers:
        labels = container.get("Labels") or {}
        names = [str(n).lstrip("/") for n in container.get("Names", [])]
        compose_project = labels.get("com.docker.compose.project")
        if compose_project in project_names or explicit_container_names.intersection(names):
            out.append(container)
    return out


def _container_name(container: dict[str, Any]) -> str:
    names = container.get("Names") or []
    if names:
        return str(names[0]).lstrip("/")
    return container.get("Id", "")[:12]


def register_compose_tools(mcp: Any, app_config: Any) -> None:
    @mcp.tool()
    def docker_compose_config(project: str) -> dict[str, Any]:
        """Validate an allowlisted Docker Compose YAML structurally and return safe summary. Does not require docker CLI."""
        compose_file = _compose_path(app_config.raw, project)
        try:
            data = _load_compose(compose_file)
        except Exception as exc:
            return {"ok": False, "project": project, "compose_file": str(compose_file), "error": str(exc)}
        services_obj = data.get("services") or {}
        volumes_obj = data.get("volumes") or {}
        networks_obj = data.get("networks") or {}
        if not isinstance(services_obj, dict):
            return {"ok": False, "project": project, "compose_file": str(compose_file), "error": "services must be a mapping"}
        services = sorted(services_obj.keys())
        volumes = sorted(volumes_obj.keys()) if isinstance(volumes_obj, dict) else []
        networks = sorted(networks_obj.keys()) if isinstance(networks_obj, dict) else []
        return {"ok": True, "project": project, "compose_file": str(compose_file), "services": services, "volumes": volumes, "networks": networks, "note": "structural YAML validation only; no Docker interpolation"}

    @mcp.tool()
    def docker_compose_ps(project: str) -> dict[str, Any]:
        """Return Docker Compose project containers using labels and configured container_names. Does not require docker CLI."""
        project_cfg = _project_config(app_config.raw, project)
        containers = _containers_for_project(project, project_cfg)
        rows = []
        for c in containers:
            labels = c.get("Labels") or {}
            rows.append({
                "name": _container_name(c),
                "service": labels.get("com.docker.compose.service"),
                "project": labels.get("com.docker.compose.project"),
                "id": c.get("Id", "")[:12],
                "image": c.get("Image"),
                "state": c.get("State"),
                "status": c.get("Status"),
                "ports": c.get("Ports", []),
            })
        return {"ok": True, "project": project, "containers": rows}

    @mcp.tool()
    def docker_compose_logs(project: str, service: str | None = None, lines: int = 100, grep: str | None = None, case_sensitive: bool = False) -> str:
        """Return recent logs from an allowlisted Docker Compose project/service using labels/container_names."""
        project_cfg = _project_config(app_config.raw, project)
        if service:
            allowed_services = set(project_cfg.get("services", []))
            if allowed_services and service not in allowed_services:
                raise SecurityError(f"Compose service not allowed for {project}: {service}")
        containers = _containers_for_project(project, project_cfg)
        if service:
            containers = [c for c in containers if (c.get("Labels") or {}).get("com.docker.compose.service") == service or service in [str(n).lstrip("/") for n in c.get("Names", [])]]
        tail = min(max(1, int(lines)), app_config.max_log_lines)
        chunks = []
        for c in containers:
            cid = c.get("Id")
            name = _container_name(c)
            status, _headers, body = _docker_request("GET", f"/containers/{cid}/logs?stdout=1&stderr=1&timestamps=1&tail={tail}")
            if status >= 400:
                continue
            text = _demux_docker_logs(body)
            for line in text.splitlines():
                chunks.append(f"[{name}] {line}")
        text = "\n".join(chunks)
        if grep:
            needle = grep if case_sensitive else grep.lower()
            filtered = []
            for line in text.splitlines():
                hay = line if case_sensitive else line.lower()
                if needle in hay:
                    filtered.append(line)
            text = "\n".join(filtered)
        return _truncate(text)
