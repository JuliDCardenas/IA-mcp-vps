from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from dari_mcp_vps.security import resolve_allowed_path


def _run_git(repo: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout.strip()


def register_git_tools(mcp: Any, app_config: Any) -> None:
    @mcp.tool(tags=["repositorios_archivos"], annotations={"readOnlyHint": True})
    def git_status(scope: str, path: str = ".") -> dict[str, Any]:
        """Return read-only git status for an allowlisted repository path."""
        repo = resolve_allowed_path(app_config.raw, scope, path)
        if not repo.is_dir():
            raise NotADirectoryError(str(repo))
        inside = _run_git(repo, ["rev-parse", "--is-inside-work-tree"])
        if inside != "true":
            raise RuntimeError("Path is not inside a git work tree")
        root = _run_git(repo, ["rev-parse", "--show-toplevel"])
        branch = _run_git(repo, ["branch", "--show-current"])
        head = _run_git(repo, ["rev-parse", "HEAD"])
        short_head = _run_git(repo, ["rev-parse", "--short", "HEAD"])
        upstream = None
        ahead_behind = None
        try:
            upstream = _run_git(repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
            counts = _run_git(repo, ["rev-list", "--left-right", "--count", "HEAD...@{u}"])
            ahead, behind = counts.split()
            ahead_behind = {"ahead": int(ahead), "behind": int(behind)}
        except Exception:
            upstream = None
            ahead_behind = None
        porcelain = _run_git(repo, ["status", "--porcelain=v1", "-b"])
        changed = [line for line in porcelain.splitlines() if line and not line.startswith("##")]
        return {
            "root": root,
            "branch": branch,
            "head": head,
            "short_head": short_head,
            "upstream": upstream,
            "ahead_behind": ahead_behind,
            "clean": len(changed) == 0,
            "changed_count": len(changed),
            "status_porcelain": porcelain,
        }

    @mcp.tool(tags=["repositorios_archivos", "mutaciones"], annotations={"readOnlyHint": False})
    def git_update_ff_only(
        idempotency_key: str,
        scope: str,
        path: str,
        target_sha: str,
        remote: str = "origin"
    ) -> dict[str, Any]:
        """Request a durably approved git fast-forward update to a specific SHA."""
        if not app_config.approval_enabled:
             return {"error": "Approval feature is disabled by configuration"}

        try:
             target = resolve_allowed_path(app_config.raw, scope, path)
             from dari_mcp_vps.security import assert_core_mutation_path
             assert_core_mutation_path(app_config.raw, scope, target, repository=True)

             scopes = app_config.raw.get('allowed_paths', {})
             is_writable = False
             if scope in scopes and scopes[scope].get('writable') is True:
                 is_writable = True

             if not is_writable:
                  return {"error": f"Scope '{scope}' is not explicitly writable."}

             if not target.is_dir():
                  return {"error": f"Path is not a directory: {target}"}

             inside = _run_git(target, ["rev-parse", "--is-inside-work-tree"])
             if inside != "true":
                 return {"error": "Path is not inside a git work tree"}

             porcelain = _run_git(target, ["status", "--porcelain=v1", "-b"])
             changed = [line for line in porcelain.splitlines() if line and not line.startswith("##")]
             if len(changed) > 0:
                 return {"error": "Working tree is dirty. Clean or stash before updating."}

             branch = _run_git(target, ["branch", "--show-current"])
             if not branch:
                 return {"error": "Detached HEAD. Require explicit branch to fast-forward."}

             head = _run_git(target, ["rev-parse", "HEAD"])

             import hashlib, json
             bl = app_config.files_blacklist if app_config.security_mode == 'blacklist' else {}
             policy_string = json.dumps({"managed_roots": sorted(bl.get("managed_roots", [])), "file_excludes": sorted(bl.get("file_excludes", []))})
             current_policy_digest = hashlib.sha256(policy_string.encode('utf-8')).hexdigest()

             parameters = {
                 "scope": scope,
                 "path": path,
                 "expected_base_sha": head,
                 "target_sha": target_sha,
                 "policy_digest": current_policy_digest,
                 "remote": remote,
                 "branch": branch
             }

             from dari_mcp_vps.tools.approval_db import create_request
             req_id, is_new = create_request(
                  app_config.approval_db_path,
                  idempotency_key,
                  "git_update_ff_only",
                  parameters,
                  app_config.approval_webhook_secret
             )

             return {
                  "request_id": req_id,
                  "status": "PENDING" if is_new else "ALREADY_EXISTS_CHECK_STATUS",
                  "message": "Approval request for git fast-forward recorded.",
                  "snapshot": {"head": head, "branch": branch, "policy_digest": current_policy_digest}
             }
        except Exception as e:
             return {"error": str(e)}

import json
import hashlib
from typing import Dict, Any, Tuple

from dari_mcp_vps.security import SecurityError, resolve_allowed_path
from dari_mcp_vps.tools.action_dispatcher import register_handler, IndeterminateStateError

def execute_git_update_ff_only(parameters: Dict[str, Any], app_config: Any) -> Tuple[bool, Dict[str, Any]]:
    """Internal handler to perform the real git fast-forward update and verify outcome."""
    scope = parameters.get("scope")
    path = parameters.get("path")
    expected_base_sha = parameters.get("expected_base_sha")
    target_sha = parameters.get("target_sha")
    expected_policy_digest = parameters.get("policy_digest")
    remote = parameters.get("remote", "origin")
    branch = parameters.get("branch")

    if not all([scope, path, expected_base_sha, target_sha, expected_policy_digest, branch]):
        return False, {"error": "Missing required parameters in request."}

    # Re-validate policy right before execution
    policy = app_config.get_tool_policy("git_update_ff_only")
    if not policy["enabled"]:
        return False, {"error": "git_update_ff_only tool is disabled by configuration."}

    try:
        target = resolve_allowed_path(app_config.raw, scope, path)
        from dari_mcp_vps.security import assert_core_mutation_path
        assert_core_mutation_path(app_config.raw, scope, target, repository=True)

        # Ensure scope is writable
        scopes = app_config.raw.get('allowed_paths', {})
        is_writable = False
        if scope in scopes and scopes[scope].get('writable') is True:
             is_writable = True

        if not is_writable:
             return False, {"error": f"Scope '{scope}' is not explicitly writable."}

        if not target.is_dir():
            return False, {"error": f"Path is not a directory: {target}"}

        bl = app_config.files_blacklist if app_config.security_mode == 'blacklist' else {}
        policy_string = json.dumps({"managed_roots": sorted(bl.get("managed_roots", [])), "file_excludes": sorted(bl.get("file_excludes", []))})
        current_policy_digest = hashlib.sha256(policy_string.encode('utf-8')).hexdigest()

        if current_policy_digest != expected_policy_digest:
            return False, {"error": "Precondition failed: Security policy has changed since approval."}

        # Validate git state
        inside = _run_git(target, ["rev-parse", "--is-inside-work-tree"])
        if inside != "true":
            return False, {"error": "Path is not inside a git work tree"}

        current_head = _run_git(target, ["rev-parse", "HEAD"])
        if current_head != expected_base_sha:
            return False, {"error": f"Precondition failed: Base SHA has changed. Expected {expected_base_sha}, got {current_head}."}

        porcelain = _run_git(target, ["status", "--porcelain=v1", "-b"])
        changed = [line for line in porcelain.splitlines() if line and not line.startswith("##")]
        if len(changed) > 0:
            return False, {"error": "Precondition failed: Working tree is dirty. Clean or stash before updating."}

        current_branch = _run_git(target, ["branch", "--show-current"])
        if not current_branch:
             return False, {"error": "Precondition failed: Detached HEAD. Require explicit branch to fast-forward."}
        if current_branch != branch:
             return False, {"error": f"Precondition failed: Current branch '{current_branch}' does not match expected branch '{branch}'."}

        # Check if remote exists
        remotes = _run_git(target, ["remote"]).splitlines()
        if remote not in remotes:
             return False, {"error": f"Remote '{remote}' does not exist."}

    except Exception as e:
        return False, {"error": f"Failed to validate pre-conditions: {e}"}

    # Fetch and check if fast-forward is possible
    try:
        import re
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", remote) or remote.startswith("-") or not re.fullmatch(r"[0-9a-f]{40}", target_sha):
             return False, {"error": "Invalid remote or full target SHA"}
        remote_url = _run_git(target, ["remote", "get-url", remote])
        if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", remote_url):
             return False, {"error": "Git mutation requires an existing credential-free HTTPS GitHub remote"}
        _run_git(target, ["-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never", "fetch", remote, branch])
        # Validate target_sha exists in remote tracking branch
        remote_sha = _run_git(target, ["rev-parse", f"{remote}/{branch}"])
        # Even if target_sha is not the absolute tip of remote/branch, it must be an ancestor of it or the tip itself,
        # AND HEAD must be an ancestor of target_sha.
        # But for simplicity, ensure the remote tracking branch can reach target_sha
        merge_base_remote_target = _run_git(target, ["merge-base", remote_sha, target_sha])
        if merge_base_remote_target != target_sha:
             return False, {"error": f"Target SHA {target_sha} is not reachable from {remote}/{branch}."}

        # Check fast-forward possibility
        merge_base_head_target = _run_git(target, ["merge-base", "HEAD", target_sha])
        if merge_base_head_target != expected_base_sha:
             return False, {"error": "Cannot fast-forward. HEAD is not an ancestor of Target SHA."}

        # Perform the actual fast-forward update
        if not remote or remote.startswith("-") or not __import__("re").fullmatch(r"[0-9a-f]{40}", target_sha):
             return False, {"error": "Remote and target SHA must be exact non-option values"}
        _run_git(target, ["-c", "core.hooksPath=/dev/null", "merge", "--ff-only", target_sha])

        new_head = _run_git(target, ["rev-parse", "HEAD"])
        if new_head != target_sha:
             raise IndeterminateStateError(f"Merge claimed success but HEAD is {new_head}, expected {target_sha}")

        return True, {"message": f"Successfully fast-forwarded to {target_sha}"}
    except Exception as e:
        raise IndeterminateStateError(f"Failed during fetch or merge: {e}")


register_handler("git_update_ff_only", execute_git_update_ff_only)
