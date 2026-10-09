import pytest
import os
from unittest.mock import patch

EXPECTED_CLASSIFICATION = {
    "system_status": True,
    "check_ports": True,
    "docker_ps": True,
    "container_inspect": True,
    "docker_logs": True,
    "docker_logs_filtered": True,
    "docker_restart": False,
    "docker_compose_config": True,
    "docker_compose_ps": True,
    "docker_compose_logs": True,
    "http_probe": True,

    "list_files": True,
    "file_info": True,
    "read_file": True,
    "read_file_range": True,
    "tail_file": True,
    "search_text": True,
    "validate_yaml": True,
    "validate_json": True,
    "git_status": True,

    "coding_repository_list": False,
    "coding_job_create": False,
    "coding_job_status": False,
    "coding_job_wait": False,
    "coding_job_result": False,
    "coding_job_changes": False,
    "coding_job_artifact": False,
    "coding_job_request_revision": False,
    "coding_job_validate_only": False,
    "coding_job_apply_mechanical_operation": False,
    "coding_job_approve_changes": False,
    "coding_job_publish_branch": False,
    "coding_job_create_pull_request": False,
    "coding_job_cancel": False,
    "coding_job_cleanup": False,
    "coding_private_job_create": False,

    "jules_request_coding_task": False,
    "jules_reply_to_task": False,
    "jules_check_task_status": False,
    "jules_get_task_activities": False,
    "discover_containers": True,
    "discover_compose_projects": True,
    "discover_http_targets": True,
    "suggest_allowlist_updates": True,
}

EXPECTED_TAGS = {
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
    "jules_get_task_activities": "jules",
    "discover_containers": "vps",
    "discover_compose_projects": "vps",
    "discover_http_targets": "vps",
    "suggest_allowlist_updates": "vps",
}

@pytest.fixture
def mock_env():
    with patch.dict(os.environ, {
        "VPS_MCP_ALLOWLIST_REPOS": "[]",
        "JULES_API_KEY": "synthetic_key",
        "N8N_WEBHOOK_URL": "http://synthetic.example",
        "N8N_WEBHOOK_KEY": "synthetic_webhook_key",
        "JULES_DB_PATH": "/tmp/test_jules.db",
        "IA_MCP_VPS_CONFIG": "config.example.yaml"
    }, clear=False):
        yield

@pytest.fixture
def test_mcp_app(mock_env):
    # Important: local import so environment variables take effect
    from dari_mcp_vps.server import mcp
    return mcp

@pytest.mark.asyncio
async def test_tool_annotations(test_mcp_app):
    tools = await test_mcp_app.list_tools()

    discovered_tool_names = {t.name for t in tools}

    # Assert all expected tools are present
    assert len(discovered_tool_names) == len(EXPECTED_CLASSIFICATION), "Tool counts mismatch."

    for t in tools:
        assert t.name in EXPECTED_CLASSIFICATION, f"Unexpected tool found: {t.name}"

        local_tool = await test_mcp_app.get_tool(t.name)


# Verify schema is intact against baseline
        schema = local_tool.parameters
        if hasattr(schema, "model_json_schema"):
            schema = schema.model_json_schema()
        assert isinstance(schema, dict), f"Tool {t.name} schema is not a dict"
        assert "properties" in schema, f"Tool {t.name} schema missing properties"

        # Verify tags are preserved
        tags = getattr(local_tool, 'tags', set())
        expected_tag = EXPECTED_TAGS[t.name]
        assert tags == {expected_tag}, f"Tool {t.name} has tags {tags}, expected {{{expected_tag}}}"

        # Verify annotations for readOnlyHint via standard MCP model_dump(by_alias=True)
        # FastMCP parses it into mcp.types.Tool properties, so we can check the serialized form.
        mcp_tool = local_tool.to_mcp_tool()
        dumped = mcp_tool.model_dump(by_alias=True, exclude_none=True)

        # In MCP v1.x, readOnlyHint should be part of the tool object? Wait, no, it's Tool.annotations.readOnlyHint
        # But we must check the serialized output.
        # Actually `local_tool.annotations` is a ToolAnnotations object.
        # `model_dump(by_alias=True)` on it should yield `{"readOnlyHint": True/False}`
        annotations_obj = getattr(local_tool, "annotations", None)
        assert annotations_obj is not None, f"Tool {t.name} is missing annotations entirely"

        serialized_annotations = annotations_obj.model_dump(by_alias=True, exclude_none=True)
        assert "readOnlyHint" in serialized_annotations, f"Tool {t.name} serialized annotations missing readOnlyHint"

        hint_value = serialized_annotations["readOnlyHint"]


        assert hint_value is EXPECTED_CLASSIFICATION[t.name], (
            f"Tool {t.name} readOnlyHint is {hint_value} but expected {EXPECTED_CLASSIFICATION[t.name]}"
        )
