import pytest
from pathlib import Path
import json
import hashlib
from dari_mcp_vps.config import AppConfig

def test_compose_up_preflight_preview(tmp_path, monkeypatch):
    import dari_mcp_vps.tools.compose_tools as ct

    import shutil, subprocess
    from types import SimpleNamespace
    monkeypatch.setattr(shutil, "which", lambda executable: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs: SimpleNamespace(returncode=0, stdout="v2.32.4", stderr=""))

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
    assert ["docker", "compose", "-f", str(compose_path), "--project-directory", str(compose_path.parent), "up", "-d", "--no-deps", "web"] in call_args

def test_compose_up_execute_isolated_service_args(tmp_path, monkeypatch):
    """Regression test ensuring docker compose up ONLY targets the approved service."""
    from dari_mcp_vps.tools.compose_tools import execute_docker_compose_up_service
    import dari_mcp_vps.tools.compose_tools as ct
    import subprocess

    compose_content = """
services:
  web:
    image: nginx:latest
  db:
    image: postgres:latest
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

    app_config = AppConfig(config, None)
    monkeypatch.setattr(AppConfig, "approval_enabled", property(lambda self: True))

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
        "allow_build": True
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

    executed_cmd = call_args[0]
    # Check that service name is explicitly at the end and no other services are there
    assert "web" in executed_cmd
    assert "db" not in executed_cmd
    assert "--build" in executed_cmd

@pytest.mark.parametrize("cli_found,returncode", [(False,0),(True,1)])
def test_preflight_unavailable_backend_never_requests_approval(tmp_path,monkeypatch,cli_found,returncode):
    import shutil, subprocess
    from types import SimpleNamespace
    import dari_mcp_vps.tools.compose_tools as ct
    from dari_mcp_vps.tools import approval_db
    monkeypatch.setattr(shutil,"which",lambda _: "/usr/bin/docker" if cli_found else None)
    monkeypatch.setattr(subprocess,"run",lambda *args,**kwargs: SimpleNamespace(returncode=returncode,stderr="unavailable"))
    monkeypatch.setattr(AppConfig,"approval_enabled",property(lambda _: True))
    monkeypatch.setattr(approval_db,"create_request",lambda *a,**k: pytest.fail("must not create approval"))
    tools={}
    class MCP:
        def tool(self,**kwargs):
            def register(fn): tools[fn.__name__]=fn;return fn
            return register
    ct.register_compose_tools(MCP(),AppConfig({},None))
    assert "error" in tools["docker_compose_up_service"]("unavailable-test","project","web")

def test_compose_host_path_mapping(tmp_path):
    import dari_mcp_vps.tools.compose_tools as ct
    worker = tmp_path / "worker"; host = tmp_path / "host"
    (worker / "app").mkdir(parents=True); (host / "app").mkdir(parents=True)
    config=AppConfig({"tools":{"docker_compose_up_service":{"host_path_map":{str(worker):str(host)}}}},None)
    assert ct._host_project_directory(config,worker/"app"/"compose.yaml") == str(host/"app")
    with pytest.raises(Exception,match="host_path_map required"):
        ct._host_project_directory(AppConfig({},None),Path("/mnt/stacks/app/compose.yaml"))
