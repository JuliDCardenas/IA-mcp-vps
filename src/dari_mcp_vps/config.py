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

def load_config(path=None):
    p = Path(path or os.getenv('IA_MCP_VPS_CONFIG', 'config.yaml')).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f'Config file not found: {p}')
    return AppConfig(yaml.safe_load(p.read_text(encoding='utf-8')) or {}, p)
