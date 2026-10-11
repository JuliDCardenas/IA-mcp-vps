import pytest
from pathlib import Path
import json
import hashlib
from dari_mcp_vps.config import AppConfig

def test_git_update_ff_only_preview(tmp_path, monkeypatch):
    import dari_mcp_vps.tools.git_tools as gt

    config = {
        "security": {"mode": "blacklist"},
        "allowed_paths": {
            "test_scope": {"root": str(tmp_path), "writable": True}
        }
    }

    monkeypatch.setattr(AppConfig, "approval_enabled", property(lambda self: True))
    monkeypatch.setattr(AppConfig, "approval_webhook_secret", property(lambda self: "secret"))
    db_path = str(tmp_path.parent / "approvals.db")
    monkeypatch.setattr(AppConfig, "approval_db_path", property(lambda self: db_path))

    app_config = AppConfig(config, None)

    from dari_mcp_vps.tools.approval_db import init_db
    init_db(app_config.approval_db_path)

    # Initialize a dummy git repo
    import subprocess
    subprocess.run(["git", "init", str(tmp_path)])
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path)
    (tmp_path / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "file.txt"], cwd=tmp_path)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path)
    subprocess.run(["git", "clean", "-fdx"], cwd=tmp_path)
    init_db(app_config.approval_db_path)

    tools = {}
    class MockMCP:
        def tool(self, tags, annotations):
            def decorator(func):
                tools[func.__name__] = func
                return func
            return decorator

    gt.register_git_tools(MockMCP(), app_config)
    git_tool = tools["git_update_ff_only"]

    res = git_tool("key_git_update_preview", "test_scope", ".", "abcdef")
    assert res.get("status") == "PENDING"
    assert "snapshot" in res

def test_git_update_ff_only_execute(tmp_path, isolated_action_dispatcher, monkeypatch):
    from dari_mcp_vps.tools.git_tools import execute_git_update_ff_only

    config = {
        "security": {"mode": "blacklist"},
        "allowed_paths": {
            "test_scope": {"root": str(tmp_path), "writable": True}
        },
        "tools": {
            "git_update_ff_only": {"enabled": True}
        }
    }

    monkeypatch.setattr(AppConfig, "approval_enabled", property(lambda self: True))
    monkeypatch.setattr(AppConfig, "approval_webhook_secret", property(lambda self: "secret"))
    db_path = str(tmp_path.parent / "approvals.db")
    monkeypatch.setattr(AppConfig, "approval_db_path", property(lambda self: db_path))

    app_config = AppConfig(config, None)

    bl = app_config.files_blacklist
    policy_string = json.dumps({"managed_roots": sorted(bl.get("managed_roots", [])), "file_excludes": sorted(bl.get("file_excludes", []))})
    current_policy_digest = hashlib.sha256(policy_string.encode('utf-8')).hexdigest()

    # Since executing a real git fetch requires a real remote, we will just mock _run_git for the execute part
    # to avoid needing a complex git setup

    import dari_mcp_vps.tools.git_tools as gt

    call_args = []
    def mock_run_git(repo, args):
        call_args.append(args)
        effective=args[:]
        while effective and effective[0]=="-c":effective=effective[2:]
        args=effective
        if args[0] == "rev-parse" and args[1] == "--is-inside-work-tree": return "true"
        if args[0] == "rev-parse" and args[1] == "HEAD":
             if any("merge" in c for c in call_args):
                  return "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
             return "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        if args[0] == "status": return ""
        if args[0] == "branch": return "main"
        if args[:2] == ["remote","get-url"]: return "https://github.com/example/sample.git"
        if args[0] == "remote": return "origin"
        if args[0] == "rev-parse" and args[1] == "origin/main": return "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        if args[0] == "merge-base":
             if args[2] == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": return "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" if args[1] == "HEAD" else "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        return ""

    monkeypatch.setattr(gt, "_run_git", mock_run_git)

    params = {
        "scope": "test_scope",
        "path": ".",
        "expected_base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "target_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "policy_digest": current_policy_digest,
        "remote": "origin",
        "branch": "main"
    }

    # Create the directory
    tmp_path.mkdir(exist_ok=True)

    success, diag = execute_git_update_ff_only(params, app_config)
    assert success is True
    assert "message" in diag
    assert ["-c", "core.hooksPath=/dev/null", "merge", "--ff-only", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"] in call_args
