from pathlib import Path
import fnmatch

class SecurityError(ValueError):
    pass

def _matches_any(value, patterns):
    return any(fnmatch.fnmatch(value, pattern) for pattern in patterns)

def is_denied_path(config, path: Path):
    path_str = str(path)
    if _matches_any(path_str, config.get('security', {}).get('deny_paths', [])):
        return True
    if path.name in config.get('security', {}).get('deny_file_names', []):
        return True
    return False

def resolve_allowed_path(config, scope, relative_path='.'):
    scopes = config.get('allowed_paths', {})
    if scope not in scopes:
        raise SecurityError(f'Scope not allowed: {scope}')
    root = Path(scopes[scope]['root']).expanduser().resolve()
    candidate = (root / relative_path).resolve()
    if not str(candidate).startswith(str(root)):
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
    if name not in set(config.get(key, [])):
        raise SecurityError(f'Not allowed: {name}')
