from pathlib import Path
import fnmatch

class SecurityError(ValueError):
    pass

def _matches_any(value, patterns):
    return any(fnmatch.fnmatch(value, pattern) for pattern in patterns)

def resolve_allowed_path(config, scope, relative_path='.'):
    scopes = config.get('allowed_paths', {})
    if scope not in scopes:
        raise SecurityError(f'Scope not allowed: {scope}')
    root = Path(scopes[scope]['root']).expanduser().resolve()
    candidate = (root / relative_path).resolve()
    if not str(candidate).startswith(str(root)):
        raise SecurityError('Path escapes allowed root')
    if _matches_any(str(candidate), config.get('security', {}).get('deny_paths', [])):
        raise SecurityError('Path is denied')
    if candidate.name in config.get('security', {}).get('deny_file_names', []):
        raise SecurityError('File name is denied')
    return candidate

def assert_allowed_name(config, key, name):
    if name not in set(config.get(key, [])):
        raise SecurityError(f'Not allowed: {name}')
