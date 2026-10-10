from pathlib import Path
import fnmatch

class SecurityError(ValueError):
    pass

def _matches_any(value, patterns):
    return any(fnmatch.fnmatch(value, pattern) for pattern in patterns)

def is_denied_path(config, path: Path):
    path_str = str(path)
    mode = config.get('security', {}).get('mode', 'legacy')

    if mode == 'blacklist':
        from dari_mcp_vps.config import AppConfig
        try:
            app_config = AppConfig(config, None)
            bl = app_config.files_blacklist
        except ValueError as e:
            raise SecurityError(f"Invalid security configuration: {e}")

        excludes = bl['file_excludes']

        default_excludes = ['.ssh', '.local', '.config', '.env*', '*.key', '*.pem', 'approvals.db', 'jules_jobs.db']
        all_excludes = excludes + default_excludes

        # Check against the full string
        if _matches_any(path_str, all_excludes):
            return True

        # Check against any part of the path
        for part in path.parts:
            if part in all_excludes or _matches_any(part, all_excludes):
                return True

        return False

    else:
        # Legacy mode
        if _matches_any(path_str, config.get('security', {}).get('deny_paths', [])):
            return True
        if path.name in config.get('security', {}).get('deny_file_names', []):
            return True
        return False

def resolve_allowed_path(config, scope, relative_path='.'):
    mode = config.get('security', {}).get('mode', 'legacy')

    if mode == 'blacklist':
        from dari_mcp_vps.config import AppConfig
        try:
            app_config = AppConfig(config, None)
            bl = app_config.files_blacklist
        except ValueError as e:
            raise SecurityError(f"Invalid security configuration: {e}")

        managed_roots = bl['managed_roots']

        scopes = config.get('allowed_paths', {})

        # In blacklist mode, check both legacy scopes and managed_roots
        root = None
        if scope in scopes:
            root = Path(scopes[scope]['root']).expanduser().resolve()
        else:
             # Find if scope matches a managed root
             for r in managed_roots:
                 if scope == r or scope == Path(r).name:
                     root = Path(r).expanduser().resolve()
                     break

             if not root:
                 # Check if scope is a valid path under a managed root
                 for r in managed_roots:
                     r_path = Path(r).expanduser().resolve()
                     try:
                         potential_root = Path(scope).expanduser().resolve()
                         if potential_root.is_relative_to(r_path):
                             root = potential_root
                             break
                     except Exception:
                         pass

             if not root:
                 # Fallback: Treat scope as a directory name inside the managed roots
                 for r in managed_roots:
                     r_path = Path(r).expanduser().resolve()
                     potential_root = (r_path / scope).resolve()
                     if potential_root.is_relative_to(r_path) and potential_root.is_dir():
                         root = potential_root
                         break

        if not root:
            raise SecurityError(f'Scope not allowed or not in managed roots: {scope}')

        candidate = (root / relative_path).resolve()

        # Verify it stays within the resolved root
        if not candidate.is_relative_to(root):
            raise SecurityError('Path escapes allowed root')

        if is_denied_path(config, candidate):
            raise SecurityError('Path is denied')

        return candidate

    else:
        # Legacy mode
        scopes = config.get('allowed_paths', {})
        if scope not in scopes:
            raise SecurityError(f'Scope not allowed: {scope}')
        root = Path(scopes[scope]['root']).expanduser().resolve()
        candidate = (root / relative_path).resolve()
        if not candidate.is_relative_to(root):
            raise SecurityError('Path escapes allowed root')
        if is_denied_path(config, candidate):
            raise SecurityError('Path is denied')
        return candidate

def assert_allowed_extension(config, scope, path: Path):
    scopes = config.get('allowed_paths', {})
    allowed_ext = set(scopes.get(scope, {}).get('extensions', []))
    if path.is_file() and allowed_ext and path.suffix not in allowed_ext:
        raise SecurityError(f'Extension not allowed: {path.suffix}')

def assert_allowed_name(config, key, name):
    mode = config.get('security', {}).get('mode', 'legacy')
    if mode == 'blacklist' and key == 'allowed_containers':
        # In blacklist mode, allow unlisted names to be checked later via Docker API
        return

    if name not in set(config.get(key, [])):
        raise SecurityError(f'Not allowed: {name}')

def assert_docker_permission(config, container_info, action: str):
    """
    Asserts if a given action ('read' or 'mutate') is allowed on a container
    based on the current security mode.
    """
    mode = config.get('security', {}).get('mode', 'legacy')

    if mode == 'legacy':
        return

    bl = config.get('security', {}).get('docker_blacklist', {})
    if not isinstance(bl, dict):
        bl = {}

    read_excludes = bl.get('read_excludes', [])
    mutate_excludes = bl.get('mutate_excludes', [])
    if not isinstance(read_excludes, list): read_excludes = []
    if not isinstance(mutate_excludes, list): mutate_excludes = []

    excludes = read_excludes if action == 'read' else read_excludes + mutate_excludes

    if action == 'mutate':
        excludes.extend(['ia-mcp-vps', 'n8n-n8n-1'])

    cid = container_info.get("Id", "")
    names = [str(n).lstrip("/") for n in container_info.get("Names", [])]
    labels = container_info.get("Labels", {})
    compose_service = labels.get("com.docker.compose.service")
    compose_project = labels.get("com.docker.compose.project")

    identities = [cid] + names
    if compose_service:
        identities.append(compose_service)
        if compose_project:
            identities.append(f"{compose_project}-{compose_service}")
            identities.append(f"{compose_project}_{compose_service}")

    for exclude in excludes:
        if _matches_any(cid, [exclude]) or _matches_any(cid[:12], [exclude]):
            raise SecurityError(f"Container denied by blacklist ({exclude}) for action {action}")
        for identity in identities:
            if _matches_any(identity, [exclude]):
                raise SecurityError(f"Container denied by blacklist ({exclude}) for action {action}")
