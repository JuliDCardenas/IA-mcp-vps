import shutil, socket, psutil

def register_system_tools(mcp, app_config):
    @mcp.tool()
    def system_status():
        """Return basic VPS health."""
        return {'cpu_percent': psutil.cpu_percent(interval=0.2), 'memory': dict(psutil.virtual_memory()._asdict()), 'boot_time': psutil.boot_time(), 'disk_root': dict(shutil.disk_usage('/')._asdict())}

    @mcp.tool()
    def check_ports(host: str, ports: list[int], timeout_s: float = 2.0):
        """Check TCP ports."""
        out = {}
        for port in ports:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout_s)
                out[port] = sock.connect_ex((host, int(port))) == 0
        return out
