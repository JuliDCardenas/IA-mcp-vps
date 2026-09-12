import subprocess
from dari_mcp_vps.security import resolve_allowed_path

def register_filesystem_tools(mcp, app_config):
    @mcp.tool()
    def list_files(scope: str, path: str = '.'):
        """List files inside an allowed scope."""
        base = resolve_allowed_path(app_config.raw, scope, path)
        return [{'name': p.name, 'is_dir': p.is_dir(), 'size': p.stat().st_size} for p in sorted(base.iterdir(), key=lambda x: x.name)]

    @mcp.tool()
    def read_file(scope: str, path: str):
        """Read a UTF-8 file inside an allowed scope."""
        target = resolve_allowed_path(app_config.raw, scope, path)
        data = target.read_bytes()
        if len(data) > app_config.max_file_bytes:
            raise ValueError('File too large')
        return data.decode('utf-8', errors='replace')

    @mcp.tool()
    def read_file_range(scope: str, path: str, start_line: int, end_line: int):
        """Read a line range."""
        lines = read_file(scope, path).splitlines()
        selected = lines[max(1, start_line)-1:end_line]
        return '\n'.join(f'{i}: {line}' for i, line in enumerate(selected, start=max(1, start_line)))

    @mcp.tool()
    def search_text(scope: str, query: str, path: str = '.'):
        """Search text with ripgrep."""
        root = resolve_allowed_path(app_config.raw, scope, path)
        proc = subprocess.run(['rg', '--line-number', '--no-heading', query, str(root)], text=True, capture_output=True, timeout=20, check=False)
        if proc.returncode not in (0, 1):
            raise RuntimeError(proc.stderr.strip())
        return proc.stdout[:20000]
