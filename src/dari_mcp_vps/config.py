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
        enabled = str(os.getenv('APPROVAL_ENABLED', self.raw.get('approvals', {}).get('enabled', 'false'))).lower() == 'true'
        if enabled:
            # Validate hard requirements for enabled
            if not self.approval_webhook_secret or not self.approval_telegram_user_id or not self.approval_telegram_chat_id:
                return False
            # Require HTTPS
            if not self.approval_n8n_webhook_url or not self.approval_n8n_webhook_url.startswith("https://"):
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
        return os.getenv('APPROVAL_N8N_WEBHOOK_KEY', self.raw.get('approvals', {}).get('n8n_webhook_key', ''))

    @property
    def approval_webhook_secret(self):
        return os.getenv('APPROVAL_WEBHOOK_SECRET', self.raw.get('approvals', {}).get('webhook_secret', ''))

    @property
    def approval_telegram_user_id(self):
        return os.getenv('APPROVAL_TELEGRAM_USER_ID', str(self.raw.get('approvals', {}).get('telegram_user_id', '')))

    @property
    def approval_telegram_chat_id(self):
        return os.getenv('APPROVAL_TELEGRAM_CHAT_ID', str(self.raw.get('approvals', {}).get('telegram_chat_id', '')))

def load_config(path=None):
    p = Path(path or os.getenv('IA_MCP_VPS_CONFIG', 'config.yaml')).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f'Config file not found: {p}')
    return AppConfig(yaml.safe_load(p.read_text(encoding='utf-8')) or {}, p)
