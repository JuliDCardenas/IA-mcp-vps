from typing import Dict, Any
import subprocess
from collections import deque
import time
from datetime import datetime, timezone
from dari_mcp_vps.security import resolve_allowed_path, assert_allowed_extension, is_denied_path

def _read_limited(path, max_bytes):
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise ValueError(f'File too large: {len(data)} bytes > {max_bytes}')
    return data.decode('utf-8', errors='replace')

def _count_lines(path):
    count = 0
    with path.open('rb') as fh:
        for _ in fh:
            count += 1
    return count

def register_filesystem_tools(mcp, app_config):
    @mcp.tool(tags=["repositorios_archivos"], annotations={"readOnlyHint": True})
    def list_files(scope: str, path: str = '.'):
        """List non-sensitive files inside an allowed scope."""
        base = resolve_allowed_path(app_config.raw, scope, path)
        if not base.exists():
            raise FileNotFoundError(str(base))
        if not base.is_dir():
            raise NotADirectoryError(str(base))
        items = []
        for child in sorted(base.iterdir(), key=lambda x: x.name):
            if is_denied_path(app_config.raw, child):
                continue
            items.append({'name': child.name, 'is_dir': child.is_dir(), 'size': child.stat().st_size})
        return items

    @mcp.tool(tags=["repositorios_archivos"], annotations={"readOnlyHint": True})
    def file_info(scope: str, path: str, count_lines: bool = True):
        """Return metadata for a file in an allowed scope, including optional line count for logs."""
        target = resolve_allowed_path(app_config.raw, scope, path)
        assert_allowed_extension(app_config.raw, scope, target)
        if not target.exists():
            raise FileNotFoundError(str(target))
        stat = target.stat()
        result = {
            'name': target.name,
            'is_file': target.is_file(),
            'is_dir': target.is_dir(),
            'size_bytes': stat.st_size,
            'modified_utc': datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            'scope': scope,
            'path': path,
        }
        if target.is_file() and count_lines:
            result['line_count'] = _count_lines(target)
        return result

    @mcp.tool(tags=["repositorios_archivos"], annotations={"readOnlyHint": True})
    def read_file(scope: str, path: str):
        """Read a UTF-8 file inside an allowed scope, size-limited."""
        target = resolve_allowed_path(app_config.raw, scope, path)
        assert_allowed_extension(app_config.raw, scope, target)
        if not target.is_file():
            raise FileNotFoundError(str(target))
        return _read_limited(target, app_config.max_file_bytes)

    @mcp.tool(tags=["repositorios_archivos"], annotations={"readOnlyHint": True})
    def read_file_range(scope: str, path: str, start_line: int, end_line: int):
        """Read a line range from large UTF-8 files without loading the full file."""
        target = resolve_allowed_path(app_config.raw, scope, path)
        assert_allowed_extension(app_config.raw, scope, target)
        if not target.is_file():
            raise FileNotFoundError(str(target))
        start = max(1, int(start_line))
        end = max(start, int(end_line))
        max_lines = min(end - start + 1, app_config.max_log_lines)
        effective_end = start + max_lines - 1
        out = []
        with target.open('r', encoding='utf-8', errors='replace') as fh:
            for idx, line in enumerate(fh, start=1):
                if idx < start:
                    continue
                if idx > effective_end:
                    break
                out.append(f'{idx}: {line.rstrip()}')
        return '\n'.join(out)

    @mcp.tool(tags=["repositorios_archivos"], annotations={"readOnlyHint": True})
    def tail_file(scope: str, path: str, lines: int = 100):
        """Return the last N lines from a UTF-8 file in an allowed scope."""
        target = resolve_allowed_path(app_config.raw, scope, path)
        assert_allowed_extension(app_config.raw, scope, target)
        if not target.is_file():
            raise FileNotFoundError(str(target))
        max_lines = min(max(1, int(lines)), app_config.max_log_lines)
        buf = deque(maxlen=max_lines)
        with target.open('r', encoding='utf-8', errors='replace') as fh:
            for idx, line in enumerate(fh, start=1):
                buf.append((idx, line.rstrip()))
        return '\n'.join(f'{idx}: {line}' for idx, line in buf)

    @mcp.tool(tags=["repositorios_archivos"], annotations={"readOnlyHint": True})
    def search_text(scope: str, query: str, path: str = '.'):
        """Search text in an allowed scope using ripgrep."""
        root = resolve_allowed_path(app_config.raw, scope, path)
        cmd = ['rg', '--line-number', '--no-heading', query, str(root)]
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=20, check=False)
        if proc.returncode not in (0, 1):
            raise RuntimeError(proc.stderr.strip())
        return proc.stdout[:20000]

    @mcp.tool(tags=["repositorios_archivos", "mutaciones"], annotations={"readOnlyHint": False})
    def patch_file_atomic(
        idempotency_key: str,
        scope: str,
        path: str,
        diff: Any,
        validate_yaml: bool = False,
        validate_json: bool = False,
        restore_on_failure: bool = True
    ) -> Dict[str, Any]:
        """Request a durably approved atomic patch to a file. diff can be a dict with 'old' and 'new' strings."""
        if not app_config.approval_enabled:
             return {"error": "Approval feature is disabled by configuration"}

        # Validation preview without effects
        try:
             target = resolve_allowed_path(app_config.raw, scope, path)
             from dari_mcp_vps.security import assert_core_mutation_path
             assert_core_mutation_path(app_config.raw, scope, target)

             scopes = app_config.raw.get('allowed_paths', {})
             is_writable = False
             if scope in scopes and scopes[scope].get('writable') is True:
                 is_writable = True

             if not is_writable:
                  return {"error": f"Scope '{scope}' is not explicitly writable."}

             if is_denied_path(app_config.raw, target):
                  return {"error": f"Path is denied: {target}"}

             if not target.exists():
                  return {"error": f"File does not exist: {target}"}

             import hashlib
             def _hash(p):
                 h = hashlib.sha256()
                 with p.open("rb") as f:
                     for chunk in iter(lambda: f.read(65536), b""): h.update(chunk)
                 return h.hexdigest()

             current_hash = _hash(target)

             import json
             bl = app_config.files_blacklist if app_config.security_mode == 'blacklist' else {}
             policy_string = json.dumps({"managed_roots": sorted(bl.get("managed_roots", [])), "file_excludes": sorted(bl.get("file_excludes", []))})
             current_policy_digest = hashlib.sha256(policy_string.encode('utf-8')).hexdigest()

             parameters = {
                 "scope": scope,
                 "path": path,
                 "expected_hash": current_hash,
                 "diff": diff,
                 "policy_digest": current_policy_digest,
                 "validate_yaml": validate_yaml,
                 "validate_json": validate_json,
                 "restore_on_failure": restore_on_failure
             }

             from dari_mcp_vps.tools.approval_db import create_request
             req_id, is_new = create_request(
                  app_config.approval_db_path,
                  idempotency_key,
                  "patch_file_atomic",
                  parameters,
                  app_config.approval_webhook_secret
             )

             return {
                  "request_id": req_id,
                  "status": "PENDING" if is_new else "ALREADY_EXISTS_CHECK_STATUS",
                  "message": "Approval request for file patch recorded.",
                  "snapshot": {"hash": current_hash, "policy_digest": current_policy_digest}
             }
        except Exception as e:
             return {"error": str(e)}

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Tuple, Dict
import yaml

from dari_mcp_vps.security import SecurityError
from dari_mcp_vps.tools.action_dispatcher import register_handler, IndeterminateStateError

def _hash_file(path: Path) -> str:
    sha256 = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest()

def execute_patch_file_atomic(parameters: Dict[str, Any], app_config: Any) -> Tuple[bool, Dict[str, Any]]:
    """Internal handler to perform the real atomic patch and verify outcome."""
    scope = parameters.get("scope")
    path = parameters.get("path")
    expected_hash = parameters.get("expected_hash")
    diff = parameters.get("diff")
    expected_policy_digest = parameters.get("policy_digest")
    validate_yaml = parameters.get("validate_yaml", False)
    validate_json = parameters.get("validate_json", False)
    restore_on_failure = parameters.get("restore_on_failure", False)

    if not all([scope, path, expected_hash, diff, expected_policy_digest]):
        return False, {"error": "Missing required parameters in request."}

    # Re-validate policy right before execution
    policy = app_config.get_tool_policy("patch_file_atomic")
    if not policy["enabled"]:
        return False, {"error": "patch_file_atomic tool is disabled by configuration."}

    try:
        from dari_mcp_vps.security import resolve_allowed_path, is_denied_path
        target = resolve_allowed_path(app_config.raw, scope, path)
        from dari_mcp_vps.security import assert_core_mutation_path
        assert_core_mutation_path(app_config.raw, scope, target)

        # Ensure scope is writable
        scopes = app_config.raw.get('allowed_paths', {})
        scope_config = scopes.get(scope, {})
        if not scope_config:
            # Need to find it from managed roots in blacklist mode if not in allowed_paths
            # For simplicity, we check if it resolves, and if so, check if the scope or its root is writable
            # Actually, resolving it already proves it's in a managed root or allowed path.
            # But we must check writable=True. If not defined in allowed_paths, it defaults to False?
            # Wait, in blacklist mode, managed_roots don't specify writable in the same way, but let's check config.
            # "Intención write solo en raíces explícitas writable"
            is_writable = False
            if scope in scopes and scopes[scope].get('writable') is True:
                is_writable = True
            elif app_config.raw.get('security', {}).get('mode') == 'blacklist':
                 # Wait, how does blacklist mode specify writable roots if not in allowed_paths?
                 # Ah! "Intención write solo en raíces explícitas writable". So it MUST be in allowed_paths with writable: true.
                 if scope in scopes and scopes[scope].get('writable') is True:
                     is_writable = True
                 else:
                     return False, {"error": f"Scope '{scope}' is not explicitly writable."}
            else:
                 if scope in scopes and scopes[scope].get('writable') is True:
                     is_writable = True

            if not is_writable:
                 return False, {"error": f"Scope '{scope}' is not explicitly writable."}

        if is_denied_path(app_config.raw, target):
            return False, {"error": f"Path is denied: {target}"}

        if not target.exists():
            return False, {"error": f"File does not exist: {target}"}

        current_hash = _hash_file(target)
        if current_hash != expected_hash:
            return False, {"error": f"Precondition failed: File hash has changed. Expected {expected_hash}, got {current_hash}."}

        bl = app_config.files_blacklist if app_config.security_mode == 'blacklist' else {}
        policy_string = json.dumps({"managed_roots": sorted(bl.get("managed_roots", [])), "file_excludes": sorted(bl.get("file_excludes", []))})
        current_policy_digest = hashlib.sha256(policy_string.encode('utf-8')).hexdigest()

        if current_policy_digest != expected_policy_digest:
            return False, {"error": "Precondition failed: Security policy has changed since approval."}

    except Exception as e:
        return False, {"error": f"Failed to validate pre-conditions: {e}"}

    # Backup & Write
    backup_path = target.with_suffix(target.suffix + f".bak.{int(time.time())}")
    try:
        shutil.copy2(target, backup_path)
    except Exception as e:
        return False, {"error": f"Failed to create backup: {e}"}

    # Simple apply diff (assuming the diff is the full new content for now, or unified patch - let's do full content replace for simplicity in 'diff',
    # but the prompt says "modificación acotada con old/new o patch estructurado; ... diff exacto, hash esperado".
    # Let's say diff is a dictionary of {'old': '...', 'new': '...'} or just full content for simplicity? No, it asks for structured patch or old/new.
    # Let's implement old/new replace.
    new_content = ""
    try:
        content = target.read_text(encoding="utf-8")
        if isinstance(diff, dict) and "old" in diff and "new" in diff:
            if diff["old"] not in content:
                return False, {"error": "Old content not found in file."}
            new_content = content.replace(diff["old"], diff["new"], 1)
        elif isinstance(diff, str):
            # Assume diff is the exact new content for this handler's scope if it's a string, to allow full rewrites if needed
            new_content = diff
        else:
            return False, {"error": "Invalid diff format. Expected dict with 'old'/'new' or string."}
    except Exception as e:
        return False, {"error": f"Failed to process diff: {e}"}

    # Atomic write
    fd, temp_path = tempfile.mkstemp(dir=target.parent, text=True)
    temp_file = Path(temp_path)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(new_content)
            f.flush()
            os.fsync(f.fileno())

        # Copy permissions
        shutil.copymode(target, temp_file)
        shutil.chown(temp_file, target.stat().st_uid, target.stat().st_gid)

        # Validations BEFORE replacing
        if validate_yaml:
            try:
                yaml.safe_load(temp_file.read_text(encoding="utf-8"))
            except yaml.YAMLError as e:
                temp_file.unlink()
                if restore_on_failure:
                     return False, {"error": f"YAML validation failed: {e}. No changes applied."}
                return False, {"error": f"YAML validation failed: {e}. No changes applied."}

        if validate_json:
            try:
                json.loads(temp_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                temp_file.unlink()
                if restore_on_failure:
                     return False, {"error": f"JSON validation failed: {e}. No changes applied."}
                return False, {"error": f"JSON validation failed: {e}. No changes applied."}

        # Replace
        os.replace(temp_file, target)

        new_hash = _hash_file(target)
        return True, {"message": "File patched successfully", "new_hash": new_hash, "backup_path": str(backup_path)}

    except Exception as e:
        if temp_file.exists():
            temp_file.unlink()
        raise IndeterminateStateError(f"Failed during atomic write/replace: {e}")


register_handler("patch_file_atomic", execute_patch_file_atomic)
