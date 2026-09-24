from __future__ import annotations

import os

from fastmcp import FastMCP

from dari_mcp_vps.config import load_config
from dari_mcp_vps.tools.system import register_system_tools
from dari_mcp_vps.tools.filesystem import register_filesystem_tools
from dari_mcp_vps.tools.validators import register_validator_tools
from dari_mcp_vps.tools.docker_tools import register_docker_tools
from dari_mcp_vps.tools.git_tools import register_git_tools
from dari_mcp_vps.tools.compose_tools import register_compose_tools
from dari_mcp_vps.tools.http_tools import register_http_tools
from dari_mcp_vps.tools.coding_jobs import register_coding_job_tools
from dari_mcp_vps.tools.private_coding_job import register_private_coding_job_tool

CONFIG = load_config()
mcp = FastMCP(CONFIG.raw.get("server", {}).get("name", "IA MCP VPS"))

register_system_tools(mcp, CONFIG)
register_filesystem_tools(mcp, CONFIG)
register_validator_tools(mcp, CONFIG)
register_docker_tools(mcp, CONFIG)
register_git_tools(mcp, CONFIG)
register_compose_tools(mcp, CONFIG)
register_http_tools(mcp, CONFIG)
register_coding_job_tools(mcp, CONFIG)
register_private_coding_job_tool(mcp, CONFIG)


def main() -> None:
    transport = os.getenv("IA_MCP_VPS_TRANSPORT", "http")
    host = os.getenv("IA_MCP_VPS_HOST", "0.0.0.0")
    port = int(os.getenv("IA_MCP_VPS_PORT", "8787"))

    if transport == "stdio":
        mcp.run()
        return

    mcp.run(transport=transport, host=host, port=port)


if __name__ == "__main__":
    main()
