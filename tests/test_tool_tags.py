import pytest
import asyncio
from unittest.mock import patch, MagicMock

# Set up test environment variables to avoid loading real config or failing
import os
os.environ["VPS_MCP_ALLOWLIST_REPOS"] = "[]"
os.environ["JULES_API_KEY"] = "synthetic_key"
os.environ["N8N_WEBHOOK_URL"] = "http://synthetic.example"
os.environ["N8N_WEBHOOK_KEY"] = "synthetic_webhook_key"
os.environ["JULES_DB_PATH"] = "/tmp/test_jules.db"
os.environ["IA_MCP_VPS_CONFIG"] = "config.example.yaml"


@pytest.fixture
def test_mcp_app():
    # Import inside fixture to ensure env vars are set before config loads
    from dari_mcp_vps.server import mcp
    return mcp


@pytest.mark.asyncio
async def test_tool_tags_metadata(test_mcp_app):
    expected_tags_map = {
        "system_status": "vps",
        "check_ports": "vps",
        "docker_ps": "vps",
        "container_inspect": "vps",
        "docker_logs": "vps",
        "docker_logs_filtered": "vps",
        "docker_restart": "vps",
        "docker_compose_config": "vps",
        "docker_compose_ps": "vps",
        "docker_compose_logs": "vps",
        "http_probe": "vps",

        "list_files": "repositorios_archivos",
        "file_info": "repositorios_archivos",
        "read_file": "repositorios_archivos",
        "read_file_range": "repositorios_archivos",
        "tail_file": "repositorios_archivos",
        "search_text": "repositorios_archivos",
        "validate_yaml": "repositorios_archivos",
        "validate_json": "repositorios_archivos",
        "git_status": "repositorios_archivos",

        "coding_repository_list": "agy",
        "coding_job_create": "agy",
        "coding_job_status": "agy",
        "coding_job_wait": "agy",
        "coding_job_result": "agy",
        "coding_job_changes": "agy",
        "coding_job_artifact": "agy",
        "coding_job_request_revision": "agy",
        "coding_job_validate_only": "agy",
        "coding_job_apply_mechanical_operation": "agy",
        "coding_job_approve_changes": "agy",
        "coding_job_publish_branch": "agy",
        "coding_job_create_pull_request": "agy",
        "coding_job_cancel": "agy",
        "coding_job_cleanup": "agy",
        "coding_private_job_create": "agy",

        "jules_request_coding_task": "jules",
        "jules_reply_to_task": "jules",
        "jules_check_task_status": "jules",
        "jules_get_task_activities": "jules"
    }

    tools = await test_mcp_app.list_tools()

    # Assert all expected tools are present
    discovered_tool_names = {t.name for t in tools}
    for expected_tool in expected_tags_map.keys():
        assert expected_tool in discovered_tool_names, f"Tool {expected_tool} is missing from discovered tools."

    # Assert exact counts to ensure no tools were silently dropped or added
    assert len(discovered_tool_names) == len(expected_tags_map), "Number of discovered tools does not match expectations."

    # Check that each tool has exactly the correct tag
    for t in tools:
        local_tool = await test_mcp_app.get_tool(t.name)
        tags = getattr(local_tool, 'tags', set())
        expected_tag = expected_tags_map[t.name]

        # Tags are represented as sets in fastmcp
        assert tags == {expected_tag}, f"Tool {t.name} has tags {tags}, expected {{{expected_tag}}}"

        # Verify schema is intact (basic sanity check)
        assert hasattr(local_tool, 'parameters'), f"Tool {t.name} is missing parameters schema"
        assert local_tool.parameters is not None