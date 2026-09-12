import subprocess
from collections import deque
from dari_mcp_vps.security import resolve_allowed_path, assert_allowed_extension, is_denied_path

def _read_limited(path, max_bytes):
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise ValueError(f'File too large: {len(data)} bytes > {max_bytes}')
    return data.decode('utf-8', errors='replace')

def register_filesystem_tools(mcp, app_config):
    @mcp.tool()
    def list_files(scope: str, path: str = '.'):
        """List non-sensitive files inside an allowed scope."""
        base = resolve_allowed_path(app_config.raw, scope, path)
        if not base.exists():
            raise FileNotFoundError(str(base))
        if not base.is_dir():
            raise NotADirectoryError(str(base))
        items = []
        for child in sorted(base.iterdir(), key=lambda x: x.name):
            if is_denied_path(app_config.raw, child):
                continue
            items.append({'name': child.name, 'is_dir': child.is_dir(), 'size': child.stat().st_size})
        return items

    @mcp.tool()
    def read_file(scope: str, path: str):
        """Read a UTF-8 file inside an allowed scope, size-limited."""
        target = resolve_allowed_path(app_config.raw, scope, path)
        assert_allowed_extension(app_config.raw, scope, target)
        if not target.is_file():
            raise FileNotFoundError(str(target))
        return _read_limited(target, app_config.max_file_bytes)

    @mcp.tool()
    def read_file_range(scope: str, path: str, start_line: int, end_line: int):
        """Read a line range from large UTF-8 files without loading the full file."""
        target = resolve_allowed_path(app_config.raw, scope, path)
        assert_allowed_extension(app_config.raw, scope, target)
        if not target.is_file():
            raise FileNotFoundError(str(target))
        start = max(1, int(start_line))
        end = max(start, int(end_line))
        max_lines = min(end - start + 1, app_config.max_log_lines)
        effective_end = start + max_lines - 1
        out = []
        with target.open('r', encoding='utf-8', errors='replace') as fh:
            for idx, line in enumerate(fh, start=1):
                if idx < start:
                    continue
                if idx > effective_end:
                    break
                out.append(f'{idx}: {line.rstrip()}')
        return '\n'.join(out)

    @mcp.tool()
    def tail_file(scope: str, path: str, lines: int = 100):
        """Return the last N lines from a UTF-8 file in an allowed scope."""
        target = resolve_allowed_path(app_config.raw, scope, path)
        assert_allowed_extension(app_config.raw, scope, target)
        if not target.is_file():
            raise FileNotFoundError(str(target))
        max_lines = min(max(1, int(lines)), app_config.max_log_lines)
        buf = deque(maxlen=max_lines)
        with target.open('r', encoding='utf-8', errors='replace') as fh:
            for idx, line in enumerate(fh, start=1):
                buf.append((idx, line.rstrip()))
        return '\n'.join(f'{idx}: {line}' for idx, line in buf)

    @mcp.tool()
    def search_text(scope: str, query: str, path: str = '.'):
        """Search text in an allowed scope using ripgrep."""
        root = resolve_allowed_path(app_config.raw, scope, path)
        cmd = ['rg', '--line-number', '--no-heading', query, str(root)]
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=20, check=False)
        if proc.returncode not in (0, 1):
            raise RuntimeError(proc.stderr.strip())
        return proc.stdout[:20000]
