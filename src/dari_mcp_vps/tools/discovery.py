from __future__ import annotations

import os
from pathlib import Path
from typing import Any
import yaml

from dari_mcp_vps.tools.docker_tools import _json, _container_name
from dari_mcp_vps.security import SecurityError, is_denied_path


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
        limit = max(1, min(limit, 100))
        try:
            containers = _json("GET", "/containers/json?all=1")
        except Exception:
            return {"ok": False, "error": "DOCKER_API_ERROR: Failed to fetch containers list."}

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
        limit = max(1, min(limit, 100))
        allowed_paths = app_config.raw.get("allowed_paths", {})
        found = []
        errors = []
        truncated = False
        partial_failure = False

        total_walk_count = 0
        MAX_WALK_BUDGET = 200

        import collections
        queue = collections.deque()

        for scope_name, scope_cfg in allowed_paths.items():
            root_path_str = scope_cfg.get("root")
            if not root_path_str:
                continue

            root_path = Path(root_path_str).expanduser().resolve()
            if not root_path.exists() or not root_path.is_dir():
                errors.append(f"SCOPE_ERROR: Scope {scope_name} root missing.")
                partial_failure = True
                continue

            queue.append((root_path, root_path, scope_cfg, scope_name))

        while queue:
            if total_walk_count >= MAX_WALK_BUDGET or len(found) >= limit:
                truncated = True
                break

            current_dir, root_path, scope_cfg, scope_name = queue.popleft()

            if not current_dir.is_relative_to(root_path) or is_denied_path(app_config.raw, current_dir):
                continue

            total_walk_count += 1

            try:
                entries = list(os.scandir(current_dir))
            except Exception:
                errors.append("SCOPE_WALK_ERROR: Permission denied or inaccessible directory during traversal.")
                partial_failure = True
                continue

            dirs = []
            files = []
            for entry in entries:
                try:
                    if entry.is_dir():
                        dirs.append(entry.path)
                    elif entry.is_file():
                        files.append(entry.name)
                except Exception:
                    continue

            dirs.sort()
            for d in dirs:
                queue.append((Path(d).resolve(), root_path, scope_cfg, scope_name))

            for f in files:
                if f in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
                    compose_path = current_dir / f
                    try:
                        resolved_path = compose_path.resolve()
                        if not resolved_path.is_relative_to(root_path):
                            continue
                        if is_denied_path(app_config.raw, resolved_path):
                            continue

                        allowed_exts = set(scope_cfg.get('extensions', []))
                        if allowed_exts and resolved_path.suffix not in allowed_exts:
                            continue

                        file_size = resolved_path.stat().st_size
                        max_size = int(app_config.raw.get('security', {}).get('max_file_bytes', 200000))
                        if file_size > max_size:
                            errors.append("COMPOSE_READ_ERROR: File size exceeds allowed limits.")
                            partial_failure = True
                            continue

                        content_yaml = resolved_path.read_text(encoding="utf-8")
                        try:
                            data = yaml.safe_load(content_yaml) or {}
                            services = list(data.get("services", {}).keys()) if isinstance(data.get("services"), dict) else []
                            networks = list(data.get("networks", {}).keys()) if isinstance(data.get("networks"), dict) else []
                            volumes = list(data.get("volumes", {}).keys()) if isinstance(data.get("volumes"), dict) else []

                            metadata_truncated = len(services) > 20 or len(networks) > 20 or len(volumes) > 20

                            found.append({
                                "path": str(resolved_path),
                                "scope": scope_name,
                                "services": services[:20],
                                "networks": networks[:20],
                                "volumes": volumes[:20],
                                "project_name": data.get("name"),
                                "metadata_truncated": metadata_truncated
                            })
                        except Exception:
                            errors.append("YAML_PARSE_ERROR: Invalid YAML format.")
                            partial_failure = True
                    except Exception:
                        errors.append("COMPOSE_READ_ERROR: Failed to resolve or read file.")
                        partial_failure = True

        # Deduplicate
        unique_found = []
        seen = set()
        for proj in found:
            if proj["path"] not in seen:
                seen.add(proj["path"])
                unique_found.append(proj)

        return {
            "ok": True,
            "count": len(unique_found),
            "truncated": truncated,
            "partial_failure": partial_failure,
            "projects": unique_found[:limit],
            "errors": list(set(errors))[:10]
        }

    @mcp.tool(tags=["vps"], annotations={"readOnlyHint": True})
    def discover_http_targets(limit: int = 50) -> dict[str, Any]:
        """Derive probable HTTP service candidates from sanitized ports/metadata.
        Does NOT actively probe any new destination.
        """
        limit = max(1, min(limit, 100))
        try:
            containers = _json("GET", "/containers/json?all=1")
        except Exception:
            return {"ok": False, "error": "DOCKER_API_ERROR: Failed to fetch containers list."}

        candidates = []
        http_ports = {80, 443, 3000, 3001, 8080, 8081, 8000, 5000, 5678, 8787, 9000, 8083}
        truncated = False

        for c in containers:
            if len(candidates) >= limit:
                truncated = True
                break

            ports = c.get("Ports", [])
            for p in ports:
                if len(candidates) >= limit:
                    truncated = True
                    break

                private_port = p.get("PrivatePort")
                public_port = p.get("PublicPort")
                protocol = p.get("Type", "tcp").lower()

                if protocol != "tcp":
                    continue

                if private_port in http_ports or public_port in http_ports:
                    name = _container_name(c)

                    if public_port:
                        scheme = "https" if private_port == 443 else "http"
                        candidates.append({
                            "container_name": name,
                            "port": public_port,
                            "ip": p.get("IP", "host.docker.internal"),
                            "confidence": "high" if private_port in {80, 443} else "medium",
                            "motive": f"Detected published {protocol} port {public_port} mapped to internal {private_port} commonly used for HTTP/S",
                            "manual_verification_required": True,
                            "scheme": scheme,
                            "published": True
                        })
                    else:
                        candidates.append({
                            "container_name": name,
                            "port": private_port,
                            "ip": "private/unreachable",
                            "confidence": "low",
                            "motive": f"Detected private {protocol} port {private_port} with no host mapping",
                            "manual_verification_required": True,
                            "scheme": "unknown",
                            "published": False
                        })

        unique_candidates = []
        seen = set()
        for cand in candidates:
            key = f"{cand['container_name']}:{cand['port']}:{cand['published']}"
            if key not in seen:
                seen.add(key)
                unique_candidates.append(cand)

        return {
            "ok": True,
            "count": len(unique_candidates),
            "truncated": truncated,
            "candidates": unique_candidates
        }

    @mcp.tool(tags=["vps"], annotations={"readOnlyHint": True})
    def suggest_allowlist_updates() -> dict[str, Any]:
        """Compare discovered candidates against configured allowlists and return valid YAML suggestions."""
        suggestions = {}
        errors = []
        partial_failure = False
        truncated = False

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
                if len(new_containers) > 50:
                    truncated = True
                suggestions["allowed_containers"] = new_containers[:50]
        except Exception:
            errors.append("DOCKER_API_ERROR: Container discovery failed.")
            partial_failure = True

        # 2. HTTP Targets
        allowed_http_targets_cfg = app_config.raw.get("allowed_http_targets", {})
        allowed_http_target_names = set(allowed_http_targets_cfg.keys())
        allowed_http_urls = [v.get("url", "").rstrip("/") for v in allowed_http_targets_cfg.values() if isinstance(v, dict)]

        try:
            from urllib.parse import urlparse
            def _is_candidate_covered(cand_ip: str, cand_port: int, allowed_urls: list[str]) -> bool:
                local_hosts = {"127.0.0.1", "::1", "localhost", "host.docker.internal", "0.0.0.0", "::"}
                for url in allowed_urls:
                    try:
                        parsed = urlparse(url)
                        url_port = parsed.port or (443 if parsed.scheme == "https" else 80)
                        if url_port != cand_port:
                            continue

                        url_host = parsed.hostname.strip("[]") if parsed.hostname else ""
                        if url_host in local_hosts and cand_ip in local_hosts:
                            return True
                        if url_host == cand_ip:
                            return True
                    except Exception:
                        pass
                return False

            http_candidates_resp = discover_http_targets(limit=100)
            if http_candidates_resp.get("ok"):
                if http_candidates_resp.get("truncated"):
                    truncated = True
                new_targets = {}
                for cand in http_candidates_resp.get("candidates", []):
                    if not cand["published"]:
                        continue # do not suggest mapping unpublished private ports

                    if _is_candidate_covered(cand["ip"], cand["port"], allowed_http_urls):
                        continue

                    target_name = f"{cand['container_name']}_{cand['port']}"
                    ip = cand["ip"]

                    # Exclude likely unreachable host loopbacks if they are not explicitly mapped
                    if ip in {"127.0.0.1", "::1"}:
                        continue
                    elif ip in {"0.0.0.0", "::"}:
                        ip = "host.docker.internal"

                    proposed_url = f"{cand['scheme']}://{ip}:{cand['port']}"

                    if target_name not in allowed_http_target_names:
                        new_targets[target_name] = {
                            "url": proposed_url,
                            "note": cand["motive"],
                            "confidence": cand["confidence"],
                            "manual_verification_required": True
                        }
                if new_targets:
                    suggestions["allowed_http_targets"] = new_targets
            else:
                errors.append("HTTP_DISCOVERY_ERROR: Failed to retrieve candidates.")
                partial_failure = True
        except Exception:
            errors.append("HTTP_DISCOVERY_ERROR: Unhandled failure in HTTP derivation.")
            partial_failure = True

        # 3. Compose Projects
        allowed_compose_projects = app_config.raw.get("allowed_compose_projects", {})
        try:
            compose_resp = discover_compose_projects(limit=50)
            if compose_resp.get("ok"):
                if compose_resp.get("partial_failure"):
                    partial_failure = True
                    errors.extend(compose_resp.get("errors", []))
                if compose_resp.get("truncated"):
                    truncated = True

                new_compose = {}

                for proj in compose_resp.get("projects", []):
                    if proj.get("metadata_truncated"):
                        truncated = True

                    already_allowed = False
                    for existing_proj in allowed_compose_projects.values():
                        if existing_proj.get("path") == proj["path"]:
                            already_allowed = True
                            break

                    if not already_allowed:
                        safe_name = proj.get("project_name") or Path(proj["path"]).parent.name
                        safe_name = safe_name.replace("-", "_").lower()
                        base_name = safe_name
                        counter = 1
                        while safe_name in allowed_compose_projects or safe_name in new_compose:
                            safe_name = f"{base_name}_{counter}"
                            counter += 1

                        new_compose[safe_name] = {"path": proj["path"]}
                if new_compose:
                    suggestions["allowed_compose_projects"] = new_compose
            else:
                errors.append("COMPOSE_DISCOVERY_ERROR: Failed to retrieve candidates.")
                partial_failure = True
        except Exception:
            errors.append("COMPOSE_DISCOVERY_ERROR: Unhandled failure in compose derivation.")
            partial_failure = True

        yaml_snippet = ""
        if suggestions:
            yaml_snippet = _safe_yaml_dump(suggestions)

        return {
            "ok": True,
            "has_suggestions": bool(suggestions),
            "truncated": truncated,
            "partial_failure": partial_failure,
            "yaml_snippet": yaml_snippet,
            "verification_steps": "Review the proposed YAML changes carefully. Ensure you want to expose these containers, paths, and HTTP targets. Merge manually into your configuration file and restart the service.",
            "errors": list(set(errors))
        }
