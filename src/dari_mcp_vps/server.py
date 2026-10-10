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
from dari_mcp_vps.tools.jules_tools import register_jules_tools
from dari_mcp_vps.tools.jules_monitor import monitor_lifespan_factory
from dari_mcp_vps.tools.discovery import register_discovery_tools
from contextlib import asynccontextmanager
import asyncio
import logging
from dari_mcp_vps.tools.approval import register_approval_tools

logger = logging.getLogger(__name__)

CONFIG = load_config()

def composed_lifespan_factory(app_config):
    jules_lifespan = monitor_lifespan_factory(app_config)

    @asynccontextmanager
    async def composed_lifespan(server):
        # Initialize approval DB
        from dari_mcp_vps.tools.approval_db import init_db
        try:
            init_db(app_config.approval_db_path)
        except Exception as e:
            logger.error(f"Failed to initialize approval DB: {e}")

        # Start approval worker
        from dari_mcp_vps.tools.approval_worker import background_approval_worker
        approval_task = asyncio.create_task(background_approval_worker(app_config))

        async with jules_lifespan(server):
            yield

        approval_task.cancel()
        try:
            await approval_task
        except asyncio.CancelledError:
            pass

    return composed_lifespan

mcp = FastMCP(CONFIG.raw.get("server", {}).get("name", "IA MCP VPS"), lifespan=composed_lifespan_factory(CONFIG))

register_system_tools(mcp, CONFIG)
register_filesystem_tools(mcp, CONFIG)
register_validator_tools(mcp, CONFIG)
register_docker_tools(mcp, CONFIG)
register_git_tools(mcp, CONFIG)
register_compose_tools(mcp, CONFIG)
register_http_tools(mcp, CONFIG)
register_coding_job_tools(mcp, CONFIG)
register_private_coding_job_tool(mcp, CONFIG)
register_jules_tools(mcp, CONFIG)
register_discovery_tools(mcp, CONFIG)
register_approval_tools(mcp, CONFIG)


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
