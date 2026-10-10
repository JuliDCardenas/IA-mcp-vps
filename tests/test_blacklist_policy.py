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
