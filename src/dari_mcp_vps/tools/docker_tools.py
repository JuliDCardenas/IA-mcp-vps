import subprocess
from dari_mcp_vps.security import assert_allowed_name

def _run(cmd, timeout=30):
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout

def register_docker_tools(mcp, app_config):
    @mcp.tool()
    def docker_ps():
        """List Docker containers."""
        return _run(['docker', 'ps', '--format', 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'])

    @mcp.tool()
    def docker_logs(container: str, lines: int = 100):
        """Return recent logs for an allowed Docker container."""
        assert_allowed_name(app_config.raw, 'allowed_containers', container)
        return _run(['docker', 'logs', '--tail', str(min(lines, app_config.max_log_lines)), container])

    @mcp.tool()
    def docker_restart(container: str):
        """Restart an allowed Docker container."""
        assert_allowed_name(app_config.raw, 'allowed_containers', container)
        return _run(['docker', 'restart', container], timeout=60)
