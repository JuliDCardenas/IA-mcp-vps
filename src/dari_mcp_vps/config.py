from pathlib import Path
import os, yaml

class AppConfig:
    def __init__(self, raw, path):
        self.raw = raw
        self.path = path
    @property
    def max_file_bytes(self):
        return int(self.raw.get('security', {}).get('max_file_bytes', 200000))
    @property
    def max_log_lines(self):
        return int(self.raw.get('security', {}).get('max_log_lines', 500))

    @property
    def jules_api_key(self):
        return os.getenv('JULES_API_KEY', self.raw.get('jules', {}).get('api_key', ''))

    @property
    def jules_api_url(self):
        return os.getenv('JULES_API_URL', self.raw.get('jules', {}).get('api_url', 'https://jules.googleapis.com/v1alpha'))

    @property
    def jules_db_path(self):
        default_db = '/var/lib/coding-jobs/jules_jobs.db'
        return os.getenv('JULES_DB_PATH', self.raw.get('jules', {}).get('db_path', default_db))

    @property
    def jules_observability_enabled(self):
        return str(self.raw.get('jules', {}).get('observability', {}).get('enabled', 'false')).lower() == 'true'

    @property
    def jules_observability_output_dir(self):
        return self.raw.get('jules', {}).get('observability', {}).get('output_dir', '/var/lib/jules-observability')

    @property
    def jules_observability_interval(self):
        return int(self.raw.get('jules', {}).get('observability', {}).get('interval_seconds', 60))

    @property
    def n8n_webhook_url(self):
        return os.getenv('N8N_WEBHOOK_URL', self.raw.get('jules', {}).get('n8n_webhook_url', ''))

    @property
    def n8n_webhook_key(self):
        return os.getenv('N8N_WEBHOOK_KEY', self.raw.get('jules', {}).get('n8n_webhook_key', ''))

    @property
    def approval_enabled(self):
        # Strictly check the os.environ so it cannot be enabled by yaml without explicitly configuring env
        enabled = str(os.getenv('APPROVAL_ENABLED', 'false')).lower() == 'true'
        if enabled:
            import urllib.parse
            import re

            # Require exact strings for auth
            if not self.approval_webhook_secret or not self.approval_n8n_webhook_key:
                return False

            # Strictly validate numeric Telegram IDs
            if not re.match(r'^-?\d+$', self.approval_telegram_user_id) or not re.match(r'^-?\d+$', self.approval_telegram_chat_id):
                return False

            # Strictly validate HTTPS URL structure
            try:
                parsed = urllib.parse.urlparse(self.approval_n8n_webhook_url)
                if parsed.scheme != "https" or not parsed.netloc:
                    return False
            except Exception:
                return False

        return enabled

    @property
    def approval_db_path(self):
        return os.getenv('APPROVAL_DB_PATH', self.raw.get('approvals', {}).get('db_path', '/var/lib/coding-jobs/approvals.db'))

    @property
    def approval_n8n_webhook_url(self):
        return os.getenv('APPROVAL_N8N_WEBHOOK_URL', self.raw.get('approvals', {}).get('n8n_webhook_url', ''))

    @property
    def approval_n8n_webhook_key(self):
        return os.getenv('APPROVAL_N8N_WEBHOOK_KEY', '')

    @property
    def approval_webhook_secret(self):
        return os.getenv('APPROVAL_WEBHOOK_SECRET', '')

    @property
    def approval_telegram_user_id(self):
        return os.getenv('APPROVAL_TELEGRAM_USER_ID', '')

    @property
    def approval_telegram_chat_id(self):
        return os.getenv('APPROVAL_TELEGRAM_CHAT_ID', '')

    @property
    def allowed_containers(self):
        return self.raw.get('allowed_containers', [])

    @property
    def allowed_http_targets(self):
        return self.raw.get('allowed_http_targets', {})

    def get_tool_policy(self, tool_name: str) -> dict:
        """
        Retrieves the strict security policy for a specific tool.
        If a policy is malformed, disabled, or missing, it fails closed.
        """
        tool_config = self.raw.get('tools', {}).get(tool_name, {})

        # Policy missing or explicitly disabled fails closed
        if not tool_config or str(tool_config.get('enabled', 'false')).lower() != 'true':
            return {"enabled": False, "requires_approval": True, "targets": []}

        # requires_approval falls back to True unless explicitly set to 'false'
        req_approval_raw = str(tool_config.get('requires_approval', 'true')).lower()
        requires_approval = req_approval_raw != 'false'

        # Intersect policy targets with global allowed_containers to strictly prevent scope expansion
        global_allowed = set(self.allowed_containers)
        raw_targets = tool_config.get('targets', [])
        if not isinstance(raw_targets, list):
            raw_targets = []

        intersected_targets = list(set(raw_targets) & global_allowed)

        return {
            "enabled": True,
            "requires_approval": requires_approval,
            "targets": intersected_targets
        }

def load_config(path=None):
    p = Path(path or os.getenv('IA_MCP_VPS_CONFIG', 'config.yaml')).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f'Config file not found: {p}')
    return AppConfig(yaml.safe_load(p.read_text(encoding='utf-8')) or {}, p)
