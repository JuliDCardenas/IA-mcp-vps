import pytest
from pathlib import Path
import json
import hashlib
from dari_mcp_vps.config import AppConfig

def test_compose_up_preflight_preview(tmp_path, monkeypatch):
    import dari_mcp_vps.tools.compose_tools as ct

    compose_content = """
services:
  web:
    image: nginx:latest
    container_name: test-web
  db:
    image: postgres:15
"""
    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text(compose_content, encoding="utf-8")

    config = {
        "security": {"mode": "blacklist"},
        "allowed_compose_projects": {
            "test_project": {"path": str(compose_path)}
        }
    }

    monkeypatch.setattr(AppConfig, "approval_enabled", property(lambda self: True))
    monkeypatch.setattr(AppConfig, "approval_webhook_secret", property(lambda self: "secret"))
    db_path = str(tmp_path.parent / "approvals.db")
    monkeypatch.setattr(AppConfig, "approval_db_path", property(lambda self: db_path))

    app_config = AppConfig(config, None)

    from dari_mcp_vps.tools.approval_db import init_db
    init_db(app_config.approval_db_path)

    tools = {}
    class MockMCP:
        def tool(self, tags, annotations):
            def decorator(func):
                tools[func.__name__] = func
                return func
            return decorator

    # Mock container lookup
    monkeypatch.setattr(ct, "_containers_for_project", lambda p, c: [{"Id": "container123", "Image": "nginx:latest", "Labels": {"com.docker.compose.service": "web"}}])

    ct.register_compose_tools(MockMCP(), app_config)
    up_tool = tools["docker_compose_up_service"]

    res = up_tool("key1", "test_project", "web")
    assert res.get("status") == "PENDING"
    assert "preflight_risks" in res

def test_compose_up_execute(tmp_path, isolated_action_dispatcher, monkeypatch):
    from dari_mcp_vps.tools.compose_tools import execute_docker_compose_up_service
    import dari_mcp_vps.tools.compose_tools as ct
    import subprocess

    compose_content = """
services:
  web:
    image: nginx:latest
"""
    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text(compose_content, encoding="utf-8")

    config = {
        "security": {"mode": "blacklist"},
        "allowed_compose_projects": {
            "test_project": {"path": str(compose_path)}
        },
        "tools": {
            "docker_compose_up_service": {"enabled": True}
        }
    }

    monkeypatch.setattr(AppConfig, "approval_enabled", property(lambda self: True))
    monkeypatch.setattr(AppConfig, "approval_webhook_secret", property(lambda self: "secret"))
    db_path = str(tmp_path.parent / "approvals.db")
    monkeypatch.setattr(AppConfig, "approval_db_path", property(lambda self: db_path))

    app_config = AppConfig(config, None)

    bl = app_config.docker_blacklist
    policy_string = json.dumps({"read": sorted(bl.get("read_excludes", [])), "mutate": sorted(bl.get("mutate_excludes", []))})
    current_policy_digest = hashlib.sha256(policy_string.encode('utf-8')).hexdigest()

    # Mock container lookup
    monkeypatch.setattr(ct, "_containers_for_project", lambda p, c: [{"Id": "container123", "Image": "nginx:latest", "Labels": {"com.docker.compose.service": "web"}}])

    import yaml
    data = yaml.safe_load(compose_content)
    compose_hash = hashlib.sha256(json.dumps(data, sort_keys=True).encode('utf-8')).hexdigest()

    params = {
        "project": "test_project",
        "service": "web",
        "compose_path": str(compose_path),
        "snapshot": {"container": {"Id": "container123", "Image": "nginx:latest"}, "compose_hash": compose_hash},
        "policy_digest": current_policy_digest,
        "allow_build": False
    }

    class MockProcess:
        returncode = 0
        stdout = "done"

    call_args = []
    def mock_run(cmd, *args, **kwargs):
        call_args.append(cmd)
        return MockProcess()

    monkeypatch.setattr(subprocess, "run", mock_run)

    success, diag = execute_docker_compose_up_service(params, app_config)
    assert success is True
    assert ["docker", "compose", "-f", str(compose_path), "up", "-d", "--no-deps"] in call_args
