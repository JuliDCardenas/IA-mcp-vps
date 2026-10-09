import pytest
import os
import yaml
from pathlib import Path
from unittest.mock import patch, MagicMock

from dari_mcp_vps.config import AppConfig
from dari_mcp_vps.tools.discovery import register_discovery_tools

# Dummy FastMCP context to collect tools
class MockMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, tags=None, annotations=None):
        def decorator(func):
            # Save function with its annotations
            func.annotations = annotations
            self.tools[func.__name__] = func
            return func
        return decorator

@pytest.fixture
def mock_app_config(tmp_path):
    scope_dir = tmp_path / "my_scope"
    scope_dir.mkdir()

    config_dict = {
        "allowed_paths": {
            "test_scope": {
                "root": str(scope_dir)
            }
        },
        "allowed_containers": ["mosquitto"],
        "allowed_compose_projects": {
            "old_proj": {"path": str(scope_dir / "old-docker-compose.yml")}
        },
        "allowed_http_targets": {
            "mcp_local": {"url": "http://127.0.0.1:8787/mcp"}
        }
    }

    mock_config = MagicMock(spec=AppConfig)
    mock_config.raw = config_dict
    return mock_config, scope_dir

@pytest.fixture
def registered_tools(mock_app_config):
    config, _ = mock_app_config
    mcp = MockMCP()
    register_discovery_tools(mcp, config)
    return mcp.tools


@patch('dari_mcp_vps.tools.discovery._json')
def test_docker_error_leakage(mock_json, registered_tools):
    mock_json.side_effect = Exception("FAKE_SECRET_IN_DOCKER_BODY")
    discover_containers = registered_tools["discover_containers"]

    res = discover_containers()
    assert res["ok"] is False
    assert "FAKE_SECRET_IN_DOCKER_BODY" not in res.get("error", "")
    assert "DOCKER_API_ERROR" in res.get("error", "")


def test_tool_annotations(registered_tools):
    for name, func in registered_tools.items():
        assert func.annotations.get("readOnlyHint") is True, f"Tool {name} must have readOnlyHint=True"

@patch('dari_mcp_vps.tools.discovery._json')
def test_discover_containers(mock_json, registered_tools):
    mock_json.return_value = [
        {
            "Id": "1234567890abcdef",
            "Names": ["/test-container"],
            "Image": "nginx:latest",
            "State": "running",
            "Status": "Up 2 hours",
            "Ports": [{"PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"}],
            "Labels": {
                "com.docker.compose.project": "test_proj",
                "com.docker.compose.service": "web",
                "secret_label": "dont_show"
            }
        }
    ] * 60 # 60 items

    discover_containers = registered_tools["discover_containers"]

    res = discover_containers(limit=50)
    assert res["ok"] is True
    assert res["count"] == 50
    assert res["truncated"] is True

    c = res["containers"][0]
    assert c["name"] == "test-container"
    assert "secret_label" not in c["compose_labels"]
    assert c["compose_labels"]["com.docker.compose.project"] == "test_proj"

@patch('dari_mcp_vps.tools.discovery._json')
def test_discover_http_targets(mock_json, registered_tools):
    mock_json.return_value = [
        {
            "Names": ["/web-app"],
            "Ports": [{"PrivatePort": 80, "PublicPort": 8080, "IP": "0.0.0.0"}]
        },
        {
            "Names": ["/db"],
            "Ports": [{"PrivatePort": 5432, "PublicPort": 5432, "IP": "0.0.0.0"}]
        }
    ]
    discover_http_targets = registered_tools["discover_http_targets"]

    res = discover_http_targets()
    assert res["ok"] is True
    assert res["count"] == 1
    assert res["candidates"][0]["container_name"] == "web-app"
    assert res["candidates"][0]["port"] == 8080
    assert "manual_verification_required" in res["candidates"][0]


def test_compose_yaml_error_leakage(registered_tools, mock_app_config):
    _, scope_dir = mock_app_config
    valid_yml = scope_dir / "docker-compose.yml"
    valid_yml.write_text("services:\n  web: [FAKE_SECRET_IN_INVALID_YAML\n")

    discover_compose_projects = registered_tools["discover_compose_projects"]
    res = discover_compose_projects()

    assert res["ok"] is True
    assert res["partial_failure"] is True
    assert any("YAML_PARSE_ERROR" in err for err in res["errors"])
    assert not any("FAKE_SECRET_IN_INVALID_YAML" in err for err in res["errors"])


def test_discover_compose_projects(registered_tools, mock_app_config):
    _, scope_dir = mock_app_config

    # Create a valid docker-compose.yml
    valid_yml = scope_dir / "docker-compose.yml"
    valid_yml.write_text("services:\n  web:\n    image: nginx\nnetworks:\n  default:\nname: my_test_proj")

    # Create an invalid symlink escaping the scope
    outside_dir = scope_dir.parent / "outside"
    outside_dir.mkdir()
    outside_yml = outside_dir / "docker-compose.yml"
    outside_yml.write_text("services: { secret: image }")

    escape_link = scope_dir / "escape-link"
    os.symlink(outside_dir, escape_link)

    discover_compose_projects = registered_tools["discover_compose_projects"]
    res = discover_compose_projects()

    assert res["ok"] is True
    assert res["count"] == 1

    proj = res["projects"][0]
    assert proj["path"] == str(valid_yml.resolve())
    assert "web" in proj["services"]
    assert "default" in proj["networks"]
    assert proj["project_name"] == "my_test_proj"

@patch('dari_mcp_vps.tools.discovery._json')
def test_suggest_allowlist_updates(mock_json, registered_tools, mock_app_config):
    config, scope_dir = mock_app_config

    mock_json.return_value = [
        {
            "Names": ["/new-app"],
            "Ports": [{"PrivatePort": 80, "PublicPort": 8080, "IP": "0.0.0.0"}]
        },
        {
            "Names": ["/mosquitto"], # Already allowed
            "Ports": [{"PrivatePort": 1883}]
        }
    ]

    # Create a new compose file
    valid_yml = scope_dir / "docker-compose.yml"
    valid_yml.write_text("services:\n  new:\n    image: redis\nname: new_app")

    suggest_allowlist_updates = registered_tools["suggest_allowlist_updates"]

    # We patch the inner discover_http_targets to avoid infinite loop / tricky mocking
    # Actually wait, we already register the tool. We can just patch `discover_http_targets.fn`
    # to return what we want.

    res = suggest_allowlist_updates()

    assert res["ok"] is True
    assert res["has_suggestions"] is True

    # Check the yaml output
    yaml_str = res["yaml_snippet"]
    parsed = yaml.safe_load(yaml_str)

    assert "new-app" in parsed["allowed_containers"]
    assert "mosquitto" not in parsed["allowed_containers"]

    assert "new-app_8080" in parsed["allowed_http_targets"]
    assert parsed["allowed_http_targets"]["new-app_8080"]["url"] == "http://host.docker.internal:8080"

    assert "new_app" in parsed["allowed_compose_projects"]
    assert parsed["allowed_compose_projects"]["new_app"]["path"] == str(valid_yml.resolve())


@patch('dari_mcp_vps.tools.discovery._json')
def test_discover_http_targets_internal_udp(mock_json, registered_tools):
    mock_json.return_value = [
        {
            "Names": ["/udp-app"],
            "Ports": [{"PrivatePort": 80, "Type": "udp"}]
        },
        {
            "Names": ["/internal-web"],
            "Ports": [{"PrivatePort": 443, "Type": "tcp"}]
        }
    ]
    discover_http_targets = registered_tools["discover_http_targets"]
    res = discover_http_targets()

    # UDP should be ignored. Internal-web should be marked unpublished.
    assert res["ok"] is True
    candidates = res["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["container_name"] == "internal-web"
    assert candidates[0]["published"] is False
    assert candidates[0]["ip"] == "private/unreachable"
