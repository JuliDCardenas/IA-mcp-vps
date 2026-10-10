import pytest
from pathlib import Path
from src.dari_mcp_vps.config import AppConfig
from src.dari_mcp_vps.security import is_denied_path, resolve_allowed_path, assert_docker_permission, SecurityError
from src.dari_mcp_vps.tools.docker_tools import execute_docker_restart
import json

@pytest.fixture
def blacklist_config():
    return AppConfig({
        'security': {
            'mode': 'blacklist',
            'files_blacklist': {
                'managed_roots': ['/tmp/managed_root_1', '/tmp/managed_root_2'],
                'file_excludes': ['secret.txt']
            },
            'docker_blacklist': {
                'read_excludes': ['bad_container'],
                'mutate_excludes': ['important_db']
            }
        }
    }, 'config.yaml')

@pytest.fixture
def legacy_config():
    return AppConfig({
        'security': {
            'mode': 'legacy',
            'deny_paths': ['*secret*']
        },
        'allowed_paths': {
            'my_scope': {'root': '/tmp/legacy_root'}
        }
    }, 'config.yaml')

def test_is_denied_path_blacklist(blacklist_config):
    assert is_denied_path(blacklist_config.raw, Path('/tmp/managed_root_1/.ssh/id_rsa')) is True
    assert is_denied_path(blacklist_config.raw, Path('/tmp/managed_root_1/secret.txt')) is True
    assert is_denied_path(blacklist_config.raw, Path('/tmp/managed_root_1/public.txt')) is False

def test_resolve_allowed_path_blacklist(blacklist_config):
    p = resolve_allowed_path(blacklist_config.raw, '/tmp/managed_root_1', 'file.txt')
    assert str(p) == str(Path('/tmp/managed_root_1/file.txt').resolve())
    with pytest.raises(SecurityError):
        resolve_allowed_path(blacklist_config.raw, '/tmp/managed_root_1', '../escaped.txt')

def test_docker_permission_blacklist(blacklist_config):
    good_container = {"Id": "111", "Names": ["/web"]}
    bad_read_container = {"Id": "222", "Names": ["/bad_container"]}
    important_container = {"Id": "333", "Names": ["/important_db"]}
    vps_container = {"Id": "444", "Names": ["/ia-mcp-vps"]}

    assert_docker_permission(blacklist_config.raw, good_container, 'mutate')
    with pytest.raises(SecurityError):
        assert_docker_permission(blacklist_config.raw, bad_read_container, 'read')
    with pytest.raises(SecurityError):
        assert_docker_permission(blacklist_config.raw, important_container, 'mutate')
    with pytest.raises(SecurityError):
        assert_docker_permission(blacklist_config.raw, vps_container, 'mutate')

def test_docker_permission_legacy(legacy_config):
    container = {"Id": "111", "Names": ["/ia-mcp-vps"]}
    assert_docker_permission(legacy_config.raw, container, 'mutate')

def test_docker_restart_denied_during_execution(monkeypatch, blacklist_config):
    def mock_find_container(name):
        return {"Id": "333", "Names": ["/important_db"], "Config": {"Image": "img"}, "State": {"StartedAt": "now"}}
    monkeypatch.setattr('src.dari_mcp_vps.tools.docker_tools._find_container', mock_find_container)

    def mock_json(method, path):
        return {"Id": "333", "Config": {"Image": "img"}, "State": {"StartedAt": "now"}}
    monkeypatch.setattr('src.dari_mcp_vps.tools.docker_tools._json', mock_json)

    blacklist_config.raw['tools'] = {'docker_restart': {'enabled': True, 'requires_approval': False, 'targets': []}}

    parameters = {
        "container": "important_db",
        "snapshot": {"Id": "333", "Image": "img", "StartedAt": "now"}
    }

    success, result = execute_docker_restart(parameters, blacklist_config)
    assert success is False
    assert "denied by blacklist" in result.get("error", "")

def test_docker_restart_policy_digest(monkeypatch, blacklist_config):
    def mock_find_container(name):
        return {"Id": "111", "Names": ["/web"], "Config": {"Image": "img"}, "State": {"StartedAt": "now"}}
    monkeypatch.setattr('src.dari_mcp_vps.tools.docker_tools._find_container', mock_find_container)

    def mock_json(method, path):
        return {"Id": "111", "Config": {"Image": "img"}, "State": {"StartedAt": "now"}}
    monkeypatch.setattr('src.dari_mcp_vps.tools.docker_tools._json', mock_json)

    blacklist_config.raw['tools'] = {'docker_restart': {'enabled': True, 'requires_approval': False, 'targets': []}}

    # Get the snapshot with the digest
    from src.dari_mcp_vps.tools.docker_tools import register_docker_tools
    import hashlib, json

    parameters = {
        "container": "web",
        "snapshot": {"Id": "111", "Image": "img", "StartedAt": "now", "PolicyDigest": "fake_hash"}
    }

    success, result = execute_docker_restart(parameters, blacklist_config)
    assert success is False
    assert "Security policy has changed" in result.get("error", "")

def test_compose_path_fallback(blacklist_config, tmp_path):
    from src.dari_mcp_vps.tools.compose_tools import _compose_path
    from src.dari_mcp_vps.security import SecurityError

    # Setup tmp managed root
    proj_dir = tmp_path / "my_proj"
    proj_dir.mkdir()
    (proj_dir / "compose.yaml").write_text("services:")

    blacklist_config.raw['security']['files_blacklist']['managed_roots'] = [str(tmp_path)]

    # Resolves to compose.yaml instead of docker-compose.yml
    p = _compose_path(blacklist_config.raw, "my_proj")
    assert p.name == "compose.yaml"

def test_is_denied_path_symlink_logic(blacklist_config, tmp_path):
    # Testing the resolve logic blocks traversal effectively
    from src.dari_mcp_vps.security import resolve_allowed_path, SecurityError

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")

    link = root / "link"
    import os
    os.symlink(str(outside), str(link))

    blacklist_config.raw['security']['files_blacklist']['managed_roots'] = [str(root)]

    # Trying to resolve the symlink escaping the root
    with pytest.raises(SecurityError):
        resolve_allowed_path(blacklist_config.raw, str(root), "link")
