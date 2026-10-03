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
    def n8n_webhook_url(self):
        return os.getenv('N8N_WEBHOOK_URL', self.raw.get('jules', {}).get('n8n_webhook_url', ''))

    @property
    def n8n_webhook_key(self):
        return os.getenv('N8N_WEBHOOK_KEY', self.raw.get('jules', {}).get('n8n_webhook_key', ''))

def load_config(path=None):
    p = Path(path or os.getenv('IA_MCP_VPS_CONFIG', 'config.yaml')).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f'Config file not found: {p}')
    return AppConfig(yaml.safe_load(p.read_text(encoding='utf-8')) or {}, p)
