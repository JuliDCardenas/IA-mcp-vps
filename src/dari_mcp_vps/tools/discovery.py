from __future__ import annotations

import os
from pathlib import Path
from typing import Any
import yaml

from dari_mcp_vps.tools.docker_tools import _json, _container_name
from dari_mcp_vps.security import SecurityError


def _safe_yaml_dump(data: Any) -> str:
    """Dump YAML safely without aliases."""
    class NoAliasDumper(yaml.SafeDumper):
        def ignore_aliases(self, data: Any) -> bool:
            return True
    return yaml.dump(data, Dumper=NoAliasDumper, default_flow_style=False, sort_keys=False)


def register_discovery_tools(mcp: Any, app_config: Any) -> None:

    @mcp.tool(tags=["vps"], annotations={"readOnlyHint": True})
    def discover_containers(limit: int = 50) -> dict[str, Any]:
        """Read-only Docker inventory discovering container candidates.
        Exposes safe fields (name/id, image, status, Compose labels, ports).
        """
        try:
            containers = _json("GET", "/containers/json?all=1")
        except Exception as exc:
            return {"ok": False, "error": f"Docker API error: {exc}"}

        out = []
        for c in containers[:limit]:
            labels = c.get("Labels") or {}
            compose_labels = {
                k: v for k, v in labels.items()
                if k.startswith("com.docker.compose.") and k in {
                    "com.docker.compose.project",
                    "com.docker.compose.service",
                }
            }
            safe_c = {
                "id": c.get("Id", "")[:12],
                "name": _container_name(c),
                "image": c.get("Image"),
                "state": c.get("State"),
                "status": c.get("Status"),
                "ports": c.get("Ports", []),
                "compose_labels": compose_labels,
            }
            out.append(safe_c)

        return {
            "ok": True,
            "count": len(out),
            "truncated": len(containers) > limit,
            "containers": out
        }

    @mcp.tool(tags=["vps"], annotations={"readOnlyHint": True})
    def discover_compose_projects(limit: int = 50) -> dict[str, Any]:
        """Detect projects/compose files only within existing authorized read scopes."""
        allowed_paths = app_config.raw.get("allowed_paths", {})
        found = []
        errors = []
        truncated = False

        for scope_name, scope_cfg in allowed_paths.items():
            root_path_str = scope_cfg.get("root")
            if not root_path_str:
                continue

            root_path = Path(root_path_str).expanduser().resolve()

            if not root_path.exists() or not root_path.is_dir():
                errors.append(f"Scope {scope_name} root not found or not a directory")
                continue

            try:
                # Bounded walk within the scope
                count = 0
                for root, dirs, files in os.walk(root_path):
                    if len(found) >= limit:
                        truncated = True
                        break

                    current_dir = Path(root).resolve()
                    # Check path traversal escape
                    if not current_dir.is_relative_to(root_path):
                        # If a symlink led us outside, ignore
                        dirs[:] = []
                        continue

                    for f in files:
                        if f in ("docker-compose.yml", "docker-compose.yaml"):
                            compose_path = current_dir / f
                            # Ensure it's not a symlink pointing outside
                            try:
                                resolved_path = compose_path.resolve()
                                if not resolved_path.is_relative_to(root_path):
                                    continue

                                # Read metadata safely
                                content = resolved_path.read_text(encoding="utf-8")
                                try:
                                    data = yaml.safe_load(content) or {}
                                    services = list(data.get("services", {}).keys()) if isinstance(data.get("services"), dict) else []
                                    networks = list(data.get("networks", {}).keys()) if isinstance(data.get("networks"), dict) else []
                                    volumes = list(data.get("volumes", {}).keys()) if isinstance(data.get("volumes"), dict) else []

                                    found.append({
                                        "path": str(resolved_path),
                                        "scope": scope_name,
                                        "services": services[:20],
                                        "networks": networks[:20],
                                        "volumes": volumes[:20],
                                        "project_name": data.get("name")
                                    })
                                except Exception as exc:
                                    errors.append(f"Error parsing {resolved_path}: {exc}")
                            except Exception as e:
                                errors.append(f"Error processing {compose_path}: {e}")

                    # Limit recursion depth implicitly by not walking too far
                    count += 1
                    if count > 100:
                        dirs[:] = [] # stop deep traversal

            except Exception as exc:
                errors.append(f"Error walking {scope_name}: {exc}")

            if truncated:
                break

        return {
            "ok": True,
            "count": len(found),
            "truncated": truncated,
            "projects": found,
            "errors": errors[:10]
        }

    @mcp.tool(tags=["vps"], annotations={"readOnlyHint": True})
    def discover_http_targets(limit: int = 50) -> dict[str, Any]:
        """Derive probable HTTP service candidates from sanitized ports/metadata.
        Does NOT actively probe any new destination.
        """
        try:
            containers = _json("GET", "/containers/json?all=1")
        except Exception as exc:
            return {"ok": False, "error": f"Docker API error: {exc}"}

        candidates = []
        http_ports = {80, 443, 3000, 3001, 8080, 8081, 8000, 5000, 5678, 8787, 9000, 8083}

        for c in containers:
            if len(candidates) >= limit:
                break

            ports = c.get("Ports", [])
            for p in ports:
                private_port = p.get("PrivatePort")
                public_port = p.get("PublicPort")

                # if mapped to a probable HTTP port or private port is common HTTP
                if private_port in http_ports or public_port in http_ports:
                    name = _container_name(c)
                    candidates.append({
                        "container_name": name,
                        "port": public_port or private_port,
                        "ip": p.get("IP", "host.docker.internal"),
                        "confidence": "high" if private_port in {80, 443} else "medium",
                        "motive": f"Detected port {private_port or public_port} commonly used for HTTP",
                        "manual_verification_required": True
                    })

        # Deduplicate
        unique_candidates = []
        seen = set()
        for cand in candidates:
            key = f"{cand['container_name']}:{cand['port']}"
            if key not in seen:
                seen.add(key)
                unique_candidates.append(cand)

        return {
            "ok": True,
            "count": len(unique_candidates),
            "candidates": unique_candidates
        }

    @mcp.tool(tags=["vps"], annotations={"readOnlyHint": True})
    def suggest_allowlist_updates() -> dict[str, Any]:
        """Compare discovered candidates against configured allowlists and return valid YAML suggestions."""
        suggestions = {}
        errors = []

        # 1. Containers
        allowed_containers = set(app_config.raw.get("allowed_containers", []))
        try:
            containers_resp = _json("GET", "/containers/json?all=1")
            new_containers = []
            for c in containers_resp:
                name = _container_name(c)
                if name not in allowed_containers:
                    new_containers.append(name)

            if new_containers:
                suggestions["allowed_containers"] = new_containers[:50]
        except Exception as exc:
            errors.append(f"Container discovery failed: {exc}")

        # 2. HTTP Targets
        allowed_http_targets = set(app_config.raw.get("allowed_http_targets", {}).keys())
        try:
            http_candidates_resp = discover_http_targets(limit=100)
            if http_candidates_resp.get("ok"):
                new_targets = {}
                for cand in http_candidates_resp.get("candidates", []):
                    target_name = f"{cand['container_name']}_{cand['port']}"
                    if target_name not in allowed_http_targets:
                        # Only propose host.docker.internal or 127.0.0.1
                        ip = cand["ip"]
                        if ip == "0.0.0.0" or ip == "::":
                            ip = "host.docker.internal"
                        new_targets[target_name] = {"url": f"http://{ip}:{cand['port']}"}
                if new_targets:
                    suggestions["allowed_http_targets"] = new_targets
        except Exception as exc:
            errors.append(f"HTTP target discovery failed: {exc}")

        # 3. Compose Projects
        allowed_compose_projects = app_config.raw.get("allowed_compose_projects", {})
        try:
            compose_resp = discover_compose_projects(limit=50)
            if compose_resp.get("ok"):
                new_compose = {}
                for proj in compose_resp.get("projects", []):
                    # Check if path is already in allowed
                    already_allowed = False
                    for existing_proj in allowed_compose_projects.values():
                        if existing_proj.get("path") == proj["path"]:
                            already_allowed = True
                            break

                    if not already_allowed:
                        # generate a safe name
                        safe_name = proj.get("project_name") or Path(proj["path"]).parent.name
                        safe_name = safe_name.replace("-", "_").lower()
                        # handle collisions
                        base_name = safe_name
                        counter = 1
                        while safe_name in allowed_compose_projects or safe_name in new_compose:
                            safe_name = f"{base_name}_{counter}"
                            counter += 1

                        new_compose[safe_name] = {"path": proj["path"]}
                if new_compose:
                    suggestions["allowed_compose_projects"] = new_compose
        except Exception as exc:
            errors.append(f"Compose project discovery failed: {exc}")


        yaml_snippet = ""
        if suggestions:
            yaml_snippet = _safe_yaml_dump(suggestions)

        return {
            "ok": True,
            "has_suggestions": bool(suggestions),
            "yaml_snippet": yaml_snippet,
            "verification_steps": "Review the proposed YAML changes carefully. Ensure you want to expose these containers, paths, and HTTP targets. Merge manually into your configuration file and restart the service.",
            "errors": errors
        }
