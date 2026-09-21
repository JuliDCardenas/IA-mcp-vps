from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from dari_mcp_vps.security import SecurityError


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


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 45) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False)


def _compose_cmd(compose_file: Path, args: list[str]) -> list[str]:
    return ["docker", "compose", "-f", str(compose_file), *args]


def _truncate(text: str, limit: int = 20000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def register_compose_tools(mcp: Any, app_config: Any) -> None:
    @mcp.tool()
    def docker_compose_config(project: str) -> dict[str, Any]:
        """Validate an allowlisted Docker Compose project and return safe summary."""
        compose_file = _compose_path(app_config.raw, project)
        proc = _run(_compose_cmd(compose_file, ["config", "--format", "json"]), cwd=compose_file.parent)
        if proc.returncode != 0:
            return {"ok": False, "project": project, "compose_file": str(compose_file), "error": _truncate(proc.stderr or proc.stdout)}
        try:
            data = json.loads(proc.stdout)
        except Exception:
            return {"ok": True, "project": project, "compose_file": str(compose_file), "raw": _truncate(proc.stdout)}
        services = sorted((data.get("services") or {}).keys())
        volumes = sorted((data.get("volumes") or {}).keys())
        networks = sorted((data.get("networks") or {}).keys())
        return {"ok": True, "project": project, "compose_file": str(compose_file), "services": services, "volumes": volumes, "networks": networks}

    @mcp.tool()
    def docker_compose_ps(project: str) -> dict[str, Any]:
        """Return Docker Compose ps for an allowlisted project."""
        compose_file = _compose_path(app_config.raw, project)
        proc = _run(_compose_cmd(compose_file, ["ps", "--format", "json"]), cwd=compose_file.parent)
        if proc.returncode != 0:
            return {"ok": False, "project": project, "compose_file": str(compose_file), "error": _truncate(proc.stderr or proc.stdout)}
        rows = []
        text = proc.stdout.strip()
        if text:
            # Compose may output JSON lines or a JSON array depending on version.
            try:
                parsed = json.loads(text)
                rows = parsed if isinstance(parsed, list) else [parsed]
            except Exception:
                for line in text.splitlines():
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        rows.append({"raw": line})
        safe_rows = []
        for row in rows:
            safe_rows.append({
                "name": row.get("Name") or row.get("Name".lower()),
                "service": row.get("Service") or row.get("Service".lower()),
                "state": row.get("State") or row.get("State".lower()),
                "status": row.get("Status") or row.get("Status".lower()),
                "publishers": row.get("Publishers") or row.get("Publishers".lower()),
            })
        return {"ok": True, "project": project, "compose_file": str(compose_file), "containers": safe_rows}

    @mcp.tool()
    def docker_compose_logs(project: str, service: str | None = None, lines: int = 100, grep: str | None = None, case_sensitive: bool = False) -> str:
        """Return recent logs from an allowlisted Docker Compose project/service."""
        compose_file = _compose_path(app_config.raw, project)
        project_cfg = _project_config(app_config.raw, project)
        if service:
            allowed_services = set(project_cfg.get("services", []))
            if allowed_services and service not in allowed_services:
                raise SecurityError(f"Compose service not allowed for {project}: {service}")
        tail = min(max(1, int(lines)), app_config.max_log_lines)
        args = ["logs", "--no-color", "--tail", str(tail)]
        if service:
            args.append(service)
        proc = _run(_compose_cmd(compose_file, args), cwd=compose_file.parent, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
        text = proc.stdout
        if grep:
            needle = grep if case_sensitive else grep.lower()
            filtered = []
            for line in text.splitlines():
                hay = line if case_sensitive else line.lower()
                if needle in hay:
                    filtered.append(line)
            text = "\n".join(filtered)
        return _truncate(text)
