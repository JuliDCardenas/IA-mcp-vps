import json
import sqlite3
import urllib.error
from unittest.mock import patch, MagicMock
import tempfile
import os
import pytest
import asyncio

from fastmcp import FastMCP
from dari_mcp_vps.config import AppConfig
from dari_mcp_vps.tools.jules_tools import register_jules_tools


@pytest.fixture
def mock_config():
    with tempfile.NamedTemporaryFile(delete=False) as tmp_db:
        db_path = tmp_db.name

    class MockAppConfig(AppConfig):
        def __init__(self):
            super().__init__({"jules": {"api_key": "test_key", "api_url": "https://test.jules.api"}}, "")

        @property
        def jules_api_key(self):
            return "test_key"

        @property
        def jules_api_url(self):
            return "https://test.jules.api"

        @property
        def jules_db_path(self):
            return db_path

    config = MockAppConfig()
    yield config
    if os.path.exists(config.jules_db_path):
        os.remove(config.jules_db_path)


@pytest.fixture
def mcp_app(mock_config):
    mcp = FastMCP("Test MCP")
    register_jules_tools(mcp, mock_config)
    # The tools are added as callables. We extract them.
    mcp_tools = asyncio.run(mcp.list_tools())
    tools = {t.name: t.fn for t in mcp_tools}
    return tools, mock_config


@patch("urllib.request.urlopen")
def test_request_coding_task_success(mock_urlopen, mcp_app):
    tools, config = mcp_app
    jules_request_coding_task = tools["jules_request_coding_task"]

    # Mock responses for 2 calls: GET /sources and POST /sessions
    # 1. GET /sources
    mock_sources_resp = MagicMock()
    mock_sources_resp.read.return_value = json.dumps({
        "sources": [{"name": "sources/github/owner/my-repo", "id": "github/owner/my-repo"}]
    }).encode("utf-8")

    # 2. POST /sessions
    mock_sessions_resp = MagicMock()
    mock_sessions_resp.read.return_value = json.dumps({
        "name": "sessions/12345",
        "id": "12345"
    }).encode("utf-8")

    mock_urlopen.side_effect = [
        MagicMock(__enter__=lambda _: mock_sources_resp, __exit__=lambda *a: None),
        MagicMock(__enter__=lambda _: mock_sessions_resp, __exit__=lambda *a: None)
    ]

    result = jules_request_coding_task("my-repo", "Refactor module X")

    assert result["status"] == "EN_PROGRESO"
    assert "task_id" in result
    assert result["jules_agent_job_id"] == "sessions/12345"

    # Verify DB update
    with sqlite3.connect(config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM jules_jobs WHERE id = ?", (result["task_id"],))
        row = cursor.fetchone()
        assert row is not None
        assert row[3] == "sessions/12345"  # jules_agent_job_id
        assert row[4] == "EN_PROGRESO"     # status


@patch("urllib.request.urlopen")
def test_request_coding_task_api_error(mock_urlopen, mcp_app):
    tools, config = mcp_app
    jules_request_coding_task = tools["jules_request_coding_task"]

    # Mock HTTP error on the first call (GET /sources)
    error = urllib.error.HTTPError("url", 500, "Internal Error", {}, None)
    error.read = MagicMock(return_value=b"Server crashed")
    mock_urlopen.side_effect = error

    result = jules_request_coding_task("my-repo", "Refactor module X")

    assert result["status"] == "FALLIDO"
    assert "task_id" in result
    assert "HTTP error 500" in result["error"]

    # Verify DB state
    with sqlite3.connect(config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM jules_jobs WHERE id = ?", (result["task_id"],))
        row = cursor.fetchone()
        assert row[0] == "FALLIDO"


def test_check_task_status(mcp_app):
    tools, config = mcp_app
    jules_check_task_status = tools["jules_check_task_status"]

    # Insert mock data manually
    with sqlite3.connect(config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO jules_jobs (id, repo_name, task_description, jules_agent_job_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("mock-task", "test-repo", "test task", "sessions/test", "EN_PROGRESO", "now", "now"))
        conn.commit()

    result = jules_check_task_status("mock-task")

    assert result["task_id"] == "mock-task"
    assert result["repo_name"] == "test-repo"
    assert result["task_description"] == "test task"
    assert result["jules_agent_job_id"] == "sessions/test"
    assert result["status"] == "EN_PROGRESO"

    not_found = jules_check_task_status("invalid-id")
    assert not_found["status"] == "NOT_FOUND"
