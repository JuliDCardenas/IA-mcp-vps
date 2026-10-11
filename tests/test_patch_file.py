import pytest
from pathlib import Path
import json
import hashlib
from dari_mcp_vps.config import AppConfig

def test_patch_file_atomic_preview(tmp_path, monkeypatch):
    import dari_mcp_vps.tools.filesystem as fs

    config = {
        "security": {"mode": "blacklist"},
        "allowed_paths": {
            "test_scope": {"root": str(tmp_path), "writable": True}
        }
    }

    monkeypatch.setattr(AppConfig, "approval_enabled", property(lambda self: True))
    monkeypatch.setattr(AppConfig, "approval_webhook_secret", property(lambda self: "secret"))
    db_path = str(tmp_path / "approvals.db")
    monkeypatch.setattr(AppConfig, "approval_db_path", property(lambda self: db_path))

    # We must patch approval_db create_request to not fail on action validation
    # Actually, patch_file_atomic must be registered in VALID_ACTIONS in approval_db.py? No, it doesn't have a static list there? Let's check.
    # Ah, "Action 'patch_file_atomic' is not in allowlist."

    import dari_mcp_vps.tools.approval_db as approval_db

    app_config = AppConfig(config, None)

    from dari_mcp_vps.tools.approval_db import init_db
    init_db(app_config.approval_db_path)

    file_path = tmp_path / "test.txt"
    file_path.write_text("hello\nworld", encoding="utf-8")

    tools = {}
    class MockMCP:
        def tool(self, tags, annotations):
            def decorator(func):
                tools[func.__name__] = func
                return func
            return decorator

    fs.register_filesystem_tools(MockMCP(), app_config)
    patch_tool = tools["patch_file_atomic"]

    res = patch_tool("key1", "test_scope", "test.txt", {"old": "hello", "new": "bye"})
    assert res.get("status") == "PENDING"
    assert "snapshot" in res

def test_patch_file_atomic_execute(tmp_path, isolated_action_dispatcher, monkeypatch):
    from dari_mcp_vps.tools.filesystem import execute_patch_file_atomic

    file_path = tmp_path / "test.txt"
    file_path.write_text("hello\nworld", encoding="utf-8")

    def _hash(p):
        import hashlib
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""): h.update(chunk)
        return h.hexdigest()

    current_hash = _hash(file_path)

    config = {
        "security": {"mode": "blacklist"},
        "allowed_paths": {
            "test_scope": {"root": str(tmp_path), "writable": True}
        },
        "tools": {
            "patch_file_atomic": {"enabled": True}
        }
    }

    monkeypatch.setattr(AppConfig, "approval_enabled", property(lambda self: True))
    monkeypatch.setattr(AppConfig, "approval_webhook_secret", property(lambda self: "secret"))
    db_path = str(tmp_path / "approvals.db")
    monkeypatch.setattr(AppConfig, "approval_db_path", property(lambda self: db_path))

    app_config = AppConfig(config, None)

    bl = app_config.files_blacklist
    policy_string = json.dumps({"managed_roots": sorted(bl.get("managed_roots", [])), "file_excludes": sorted(bl.get("file_excludes", []))})
    current_policy_digest = hashlib.sha256(policy_string.encode('utf-8')).hexdigest()

    params = {
        "scope": "test_scope",
        "path": "test.txt",
        "expected_hash": current_hash,
        "diff": {"old": "hello\nworld", "new": "bye\nworld"},
        "policy_digest": current_policy_digest
    }

    success, diag = execute_patch_file_atomic(params, app_config)
    assert success is True
    assert "new_hash" in diag

    assert file_path.read_text(encoding="utf-8") == "bye\nworld"
