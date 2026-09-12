import json, yaml
from dari_mcp_vps.security import resolve_allowed_path

def register_validator_tools(mcp, app_config):
    @mcp.tool()
    def validate_yaml(scope: str, path: str):
        """Validate YAML."""
        target = resolve_allowed_path(app_config.raw, scope, path)
        try:
            yaml.safe_load(target.read_text(encoding='utf-8'))
            return {'ok': True, 'error': None}
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}

    @mcp.tool()
    def validate_json(scope: str, path: str):
        """Validate JSON."""
        target = resolve_allowed_path(app_config.raw, scope, path)
        try:
            json.loads(target.read_text(encoding='utf-8'))
            return {'ok': True, 'error': None}
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}
