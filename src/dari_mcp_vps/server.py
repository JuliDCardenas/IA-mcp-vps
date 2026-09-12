from mcp.server.fastmcp import FastMCP
from dari_mcp_vps.config import load_config
from dari_mcp_vps.tools.system import register_system_tools
from dari_mcp_vps.tools.filesystem import register_filesystem_tools
from dari_mcp_vps.tools.validators import register_validator_tools
from dari_mcp_vps.tools.docker_tools import register_docker_tools

CONFIG = load_config()
mcp = FastMCP(CONFIG.raw.get('server', {}).get('name', 'IA MCP VPS'))

register_system_tools(mcp, CONFIG)
register_filesystem_tools(mcp, CONFIG)
register_validator_tools(mcp, CONFIG)
register_docker_tools(mcp, CONFIG)

if __name__ == '__main__':
    mcp.run()
