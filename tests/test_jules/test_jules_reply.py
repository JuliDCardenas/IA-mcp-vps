import json
import sqlite3
import urllib.error
from unittest.mock import patch, MagicMock
import tempfile
import os
import pytest

from fastmcp import FastMCP
from dari_mcp_vps.config import AppConfig
from dari_mcp_vps.tools.jules_db import init_db, create_job, update_job_remote_id, update_job_status
from dari_mcp_vps.tools.jules_tools import register_jules_tools


@pytest.fixture
def mock_config():
    with tempfile.NamedTemporaryFile(delete=False) as tmp_db:
        db_path = tmp_db.name

    class MockAppConfig(AppConfig):
        def __init__(self):
            super().__init__({"jules": {"api_key": "test_key", "api_url": "https://test.jules.api"}}, "")
            self._key = "test_key"
            self._db_path = db_path

        @property
        def jules_api_key(self):
            return self._key

        @property
        def jules_api_url(self):
            return "https://test.jules.api"

        @property
        def jules_db_path(self):
            return self._db_path

    config = MockAppConfig()
    init_db(config.jules_db_path)
    yield config
    if os.path.exists(config.jules_db_path):
        os.remove(config.jules_db_path)


@pytest.fixture
def mcp_app(mock_config):
    mcp = FastMCP("Test MCP")
    register_jules_tools(mcp, mock_config)

    # Init DB lazily
    tools = {t.name: t.fn for t in mcp._tool_manager.get_tools()} if hasattr(mcp, '_tool_manager') else {}
    if not tools:
        import asyncio
        mcp_tools = asyncio.run(mcp.list_tools())
        tools = {t.name: t.fn for t in mcp_tools}
    return tools, mock_config

@patch("urllib.request.urlopen")
def test_reply_to_task_success(mock_urlopen, mcp_app):
    tools, config = mcp_app
    reply_tool = tools["jules_reply_to_task"]

    job_id = create_job(config.jules_db_path, "repo", "task")
    update_job_remote_id(config.jules_db_path, job_id, "sessions/123")

    resp = MagicMock()
    resp.read.return_value = b"{}"
    mock_urlopen.return_value.__enter__.return_value = resp

    result = reply_tool(job_id, "Fix this issue")
    assert result["status"] == "SENT"
    assert result["jules_agent_job_id"] == "sessions/123"

    req_arg = mock_urlopen.call_args[0][0]
    assert req_arg.full_url == "https://test.jules.api/sessions/123:sendMessage"
    assert req_arg.get_header("X-goog-api-key") == "test_key"
    payload = json.loads(req_arg.data.decode("utf-8"))
    assert payload["prompt"] == "Fix this issue"

    # Verify followup_pending_since was set
    with sqlite3.connect(config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT followup_pending_since FROM jules_jobs WHERE id = ?", (job_id,))
        row = cursor.fetchone()
        assert row is not None
        assert row[0] is not None

def test_reply_to_task_not_found(mcp_app):
    tools, config = mcp_app
    reply_tool = tools["jules_reply_to_task"]

    result = reply_tool("invalid_id", "msg")
    assert result["status"] == "NOT_FOUND"

@patch("urllib.request.urlopen")
def test_reply_to_task_api_error_sanitization(mock_urlopen, mcp_app):
    tools, config = mcp_app
    reply_tool = tools["jules_reply_to_task"]

    job_id = create_job(config.jules_db_path, "repo", "task")
    update_job_remote_id(config.jules_db_path, job_id, "sessions/123")

    # Simulate a generic network exception that leaks credentials
    generic_error = Exception(f"Connection dropped leaking {config.jules_api_key}")
    mock_urlopen.side_effect = generic_error

    result = reply_tool(job_id, "msg")
    assert result["status"] == "DESCONOCIDO"
    assert "***REDACTED***" in result["error"]
    assert config.jules_api_key not in result["error"]

@patch("urllib.request.urlopen")
def test_reply_to_task_api_error_truncation(mock_urlopen, mcp_app):
    tools, config = mcp_app
    reply_tool = tools["jules_reply_to_task"]

    job_id = create_job(config.jules_db_path, "repo", "task")
    update_job_remote_id(config.jules_db_path, job_id, "sessions/123")

    # Verify that the key is redacted even if the original error is longer than the truncation limit
    long_prefix = "A" * 195
    error_body = f"{long_prefix} {config.jules_api_key} trailing data".encode("utf-8")

    error = urllib.error.HTTPError("url", 400, "Bad Request", {}, None)
    error.read = MagicMock(return_value=error_body)
    mock_urlopen.side_effect = error

    result = reply_tool(job_id, "msg")
    assert result["status"] == "ERROR"
    assert config.jules_api_key not in result["error"]
    # Check that it did truncate correctly and redacted the part it could
    assert len(result["error"]) <= 250 # 200 body + prefix

    # Verify followup_pending_since was NOT set
    with sqlite3.connect(config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT followup_pending_since FROM jules_jobs WHERE id = ?", (job_id,))
        row = cursor.fetchone()
        assert row is not None
        assert row[0] is None
