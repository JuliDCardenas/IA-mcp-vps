from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dari_mcp_vps.security import SecurityError
from dari_mcp_vps.tools.docker_tools import _json, _docker_request, _demux_docker_logs


def _project_config(config: dict[str, Any], project: str) -> dict[str, Any]:
    mode = config.get('security', {}).get('mode', 'legacy')
    projects = config.get("allowed_compose_projects", {})

    if mode == 'blacklist':
        if project in projects:
            return projects[project]
        # In blacklist mode, project config can be inferred implicitly
        return {"implicit": True}

    if project not in projects:
        raise SecurityError(f"Compose project not allowed: {project}")
    return projects[project]


def _compose_path(config: dict[str, Any], project: str) -> Path:
    mode = config.get('security', {}).get('mode', 'legacy')

    if mode == 'blacklist':
        p_cfg = _project_config(config, project)
        if "implicit" in p_cfg:
             from dari_mcp_vps.security import SecurityError, resolve_allowed_path
             path = None

             # Standard Compose precedence; every candidate uses the same
             # managed-root, canonical-path and exclusion checks.
             for filename in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"):
                 try:
                     candidate = resolve_allowed_path(config, project, filename)
                     if candidate.exists():
                         path = candidate
                         break
                 except Exception:
                     pass

             if not path:
                 raise SecurityError(f"Compose project file not found or not allowed in managed roots: {project}")

             return path

    value = _project_config(config, project).get("path")
    if not value:
        raise SecurityError(f"Compose project has no path: {project}")
    path = Path(value).resolve()
    if not path.exists():
        raise FileNotFoundError(str(path))
    return path


def _host_project_directory(app_config: Any, compose_path: Path) -> str:
    """Map the trusted container path to the Docker daemon's host path.

    Caller cannot provide mappings. Mirror host destination read-only in worker
    so Compose can resolve .env/build context without using /mnt/stacks paths
    as host bind-mount sources.
    """
    mappings = app_config.raw.get("tools", {}).get("docker_compose_up_service", {}).get("host_path_map", {})
    if not isinstance(mappings, dict):
        raise SecurityError("host_path_map must be an object")
    parent = compose_path.resolve().parent
    for source, destination in sorted(mappings.items(), key=lambda pair: len(pair[0]), reverse=True):
        if not isinstance(source, str) or not isinstance(destination, str) or not Path(source).is_absolute() or not Path(destination).is_absolute():
            raise SecurityError("host_path_map requires absolute paths")
        try:
            relative = parent.relative_to(Path(source).resolve())
        except ValueError:
            continue
        mapped = Path(destination) / relative
        if not mapped.is_dir():
            raise SecurityError("Host project directory is not accessible in worker; mount it read-only")
        return str(mapped)
    if str(parent).startswith("/mnt/stacks/"):
        raise SecurityError("Compose host_path_map required for /mnt/stacks")
    return str(parent)


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
    @mcp.tool(tags=["vps"], annotations={"readOnlyHint": True})
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

    @mcp.tool(tags=["vps"], annotations={"readOnlyHint": True})
    def docker_compose_ps(project: str) -> dict[str, Any]:
        """Return Docker Compose project containers using labels and configured container_names. Does not require docker CLI."""
        project_cfg = _project_config(app_config.raw, project)
        containers = _containers_for_project(project, project_cfg)

        from dari_mcp_vps.security import assert_docker_permission, SecurityError
        allowed_containers = []
        for c in containers:
            try:
                assert_docker_permission(app_config.raw, c, 'read')
                allowed_containers.append(c)
            except SecurityError:
                pass

        rows = []
        for c in allowed_containers:
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

    @mcp.tool(tags=["vps"], annotations={"readOnlyHint": True})
    def docker_compose_logs(project: str, service: str | None = None, lines: int = 100, grep: str | None = None, case_sensitive: bool = False) -> str:
        """Return recent logs from an allowlisted Docker Compose project/service using labels/container_names."""
        project_cfg = _project_config(app_config.raw, project)
        if service:
            allowed_services = set(project_cfg.get("services", []))
            if allowed_services and service not in allowed_services:
                raise SecurityError(f"Compose service not allowed for {project}: {service}")
        containers = _containers_for_project(project, project_cfg)

        from dari_mcp_vps.security import assert_docker_permission, SecurityError
        allowed_containers = []
        for c in containers:
            try:
                assert_docker_permission(app_config.raw, c, 'read')
                allowed_containers.append(c)
            except SecurityError:
                pass

        containers = allowed_containers
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

    @mcp.tool(tags=["vps"], annotations={"readOnlyHint": True})
    def deployment_preflight(project: str, service: str) -> dict[str, Any]:
        """Perform a read-only preflight check for a deployment of a single Compose service."""
        try:
             import shutil
             if not shutil.which("docker"):
                  return {"error": "Preflight failed: docker CLI not found on backend."}
             backend = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, check=False, timeout=10)
             if backend.returncode != 0:
                  return {"error": "Preflight failed: docker compose plugin unavailable."}
             compose_path = _compose_path(app_config.raw, project)
             data = _load_compose(compose_path)

             import re
             if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", service):
                  return {"error": "Invalid service name"}
             if service not in data.get("services", {}):
                  return {"error": f"Service '{service}' not found in project '{project}'"}

             import hashlib, json
             compose_hash = hashlib.sha256(json.dumps(data, sort_keys=True).encode('utf-8')).hexdigest()

             project_cfg = _project_config(app_config.raw, project)
             containers = _containers_for_project(project, project_cfg)

             svc_container = None
             for c in containers:
                  if (c.get("Labels") or {}).get("com.docker.compose.service") == service:
                       svc_container = c
                       break

             snapshot = {}
             if svc_container:
                  snapshot = {
                       "Id": svc_container.get("Id"),
                       "Image": svc_container.get("Image"),
                  }

             service_config = data["services"][service]
             risks = []
             if "build" in service_config:
                  risks.append("Service uses 'build'. Requires 'allow_build' flag if image needs updating.")
             if service_config.get("privileged"):
                  risks.append("Service runs in privileged mode.")

             volumes = service_config.get("volumes", [])
             for v in volumes:
                  if isinstance(v, str) and (v.startswith("/") or v.startswith("./") or v.startswith("../")):
                       risks.append(f"Service mounts host path: {v}")
                       if "docker.sock" in v:
                            risks.append("CRITICAL RISK: Service mounts Docker socket.")

             return {
                  "ok": True,
                  "project": project,
                  "service": service,
                  "compose_path": str(compose_path),
                  "project_directory": _host_project_directory(app_config, compose_path),
                  "snapshot": {"container": snapshot, "compose_hash": compose_hash},
                  "risks": risks,
                  "message": "Preflight complete. Review risks before approving deployment."
             }
        except Exception as e:
             return {"error": str(e)}

    @mcp.tool(tags=["vps", "mutaciones"], annotations={"readOnlyHint": False})
    def docker_compose_up_service(
        idempotency_key: str,
        project: str,
        service: str,
        allow_build: bool = False
    ) -> dict[str, Any]:
        """Request a durably approved deployment (compose up) for a SINGLE service."""
        if not app_config.approval_enabled:
             return {"error": "Approval feature is disabled by configuration"}

        try:
             preflight = deployment_preflight(project, service)
             if "error" in preflight:
                  return preflight

             import hashlib, json
             bl = app_config.docker_blacklist if app_config.security_mode == 'blacklist' else {}
             policy_string = json.dumps({"read": sorted(bl.get("read_excludes", [])), "mutate": sorted(bl.get("mutate_excludes", []))})
             current_policy_digest = hashlib.sha256(policy_string.encode('utf-8')).hexdigest()

             parameters = {
                 "project": project,
                 "service": service,
                 "compose_path": preflight["compose_path"],
                 "project_directory": preflight["project_directory"],
                 "snapshot": preflight["snapshot"],
                 "policy_digest": current_policy_digest,
                 "allow_build": allow_build
             }

             from dari_mcp_vps.tools.approval_db import create_request
             req_id, is_new = create_request(
                  app_config.approval_db_path,
                  idempotency_key,
                  "docker_compose_up_service",
                  parameters,
                  app_config.approval_webhook_secret
             )

             return {
                  "request_id": req_id,
                  "status": "PENDING" if is_new else "ALREADY_EXISTS_CHECK_STATUS",
                  "message": f"Approval request for compose up of service '{service}' recorded.",
                  "preflight_risks": preflight["risks"]
             }
        except Exception as e:
             return {"error": str(e)}

import json
import hashlib
import subprocess
from typing import Dict, Tuple

from dari_mcp_vps.tools.action_dispatcher import register_handler, IndeterminateStateError

def execute_docker_compose_up_service(parameters: Dict[str, Any], app_config: Any) -> Tuple[bool, Dict[str, Any]]:
    """Internal handler to perform docker compose up for a single service."""
    project = parameters.get("project")
    service = parameters.get("service")
    expected_snapshot = parameters.get("snapshot")
    compose_path_str = parameters.get("compose_path")
    expected_policy_digest = parameters.get("policy_digest")
    allow_build = parameters.get("allow_build", False)

    if not all([project, service, expected_snapshot, compose_path_str, expected_policy_digest]):
        return False, {"error": "Missing required parameters in request."}

    # Re-validate policy right before execution
    policy = app_config.get_tool_policy("docker_compose_up_service")
    if not policy["enabled"]:
        return False, {"error": "docker_compose_up_service tool is disabled by configuration."}

    try:
        from dari_mcp_vps.security import SecurityError
        # Re-resolve compose file path to ensure it is still allowed
        current_compose_path = _compose_path(app_config.raw, project)
        if str(current_compose_path) != compose_path_str:
            return False, {"error": "Precondition failed: Compose file path changed since approval."}

        data = _load_compose(current_compose_path)
        services = data.get("services", {})
        if service not in services:
             return False, {"error": f"Service '{service}' not found in Compose file."}

        # Check blacklist policy digest
        bl = app_config.docker_blacklist if app_config.security_mode == 'blacklist' else {}
        policy_string = json.dumps({"read": sorted(bl.get("read_excludes", [])), "mutate": sorted(bl.get("mutate_excludes", []))})
        current_policy_digest = hashlib.sha256(policy_string.encode('utf-8')).hexdigest()

        if current_policy_digest != expected_policy_digest:
            return False, {"error": "Precondition failed: Security policy has changed since approval."}

        # Check explicit exclusions just in case
        excludes = bl.get("mutate_excludes", []) + ['ia-mcp-vps', 'n8n-n8n-1']
        identities = [service, f"{project}-{service}", f"{project}_{service}"]
        # We also need container names from compose
        container_name = services[service].get("container_name")
        if container_name:
             identities.append(container_name)

        for exclude in excludes:
             from dari_mcp_vps.security import _matches_any
             for identity in identities:
                  if _matches_any(identity, [exclude]):
                       return False, {"error": f"Service denied by blacklist ({exclude})"}

        # Snapshot check: ensure current state matches the snapshot at request time
        # Specifically, check the image and compose version hash or similar,
        # or just container ID if it existed.
        # Actually the snapshot we capture contains the current state.

        project_cfg = _project_config(app_config.raw, project)
        containers = _containers_for_project(project, project_cfg)

        # Filter for the specific service
        svc_container = None
        for c in containers:
             if (c.get("Labels") or {}).get("com.docker.compose.service") == service:
                  svc_container = c
                  break

        current_snapshot = {}
        if svc_container:
            current_snapshot = {
                 "Id": svc_container.get("Id"),
                 "Image": svc_container.get("Image"),
            }

        if current_snapshot != expected_snapshot.get("container", {}):
             return False, {"error": "Precondition failed: Container state has changed since approval."}

        import yaml
        compose_hash = hashlib.sha256(json.dumps(data, sort_keys=True).encode('utf-8')).hexdigest()
        if compose_hash != expected_snapshot.get("compose_hash"):
             return False, {"error": "Precondition failed: Compose file has been modified since approval."}

    except Exception as e:
        return False, {"error": f"Failed to validate pre-conditions: {e}"}

    try:
        project_directory = _host_project_directory(app_config, current_compose_path)
        if parameters.get("project_directory", str(current_compose_path.parent)) != project_directory:
            return False, {"error": "Approved host project directory changed."}
    except Exception as e:
        return False, {"error": str(e)}

    # Execute docker compose up
    cmd = ["docker", "compose", "-f", str(current_compose_path), "--project-directory", project_directory, "up", "-d", "--no-deps"]
    if allow_build:
         cmd.append("--build")
    cmd.append(service)

    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=120, check=False)

        if proc.returncode != 0:
            raise IndeterminateStateError(f"docker compose up failed. Exit code {proc.returncode}. Error: {proc.stderr}")

        return True, {"message": f"Compose command completed for service {service}", "project": project, "service": service, "verification": "health_check_pending"}
    except IndeterminateStateError:
        raise
    except Exception as e:
        raise IndeterminateStateError(f"Failed during execution: {e}")

register_handler("docker_compose_up_service", execute_docker_compose_up_service)
