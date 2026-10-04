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
            self._key = "test_key"
            self._db_path = db_path

        @property
        def jules_api_key(self):
            return self._key

        @jules_api_key.setter
        def jules_api_key(self, value):
            self._key = value

        @property
        def jules_api_url(self):
            return "https://test.jules.api"

        @property
        def jules_db_path(self):
            return self._db_path

        @jules_db_path.setter
        def jules_db_path(self, value):
            self._db_path = value

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
def test_missing_config(mock_urlopen, mcp_app):
    tools, config = mcp_app
    jules_request_coding_task = tools["jules_request_coding_task"]

    # Temporarily remove API key
    original_key = config.jules_api_key
    config.jules_api_key = ""

    result = jules_request_coding_task("my-repo", "Refactor module X")
    assert result["status"] == "ERROR"
    assert "JULES_API_KEY is not configured" in result["error"]

    config.jules_api_key = original_key

@patch("urllib.request.urlopen")
def test_lazy_init_failure(mock_urlopen, mcp_app):
    tools, config = mcp_app
    jules_request_coding_task = tools["jules_request_coding_task"]

    # Point to an invalid directory to simulate unwriteable path
    original_path = config.jules_db_path
    config.jules_db_path = "/nonexistent_root_dir/some.db"

    # Reset lazy initialization state by extracting the _get_db function from the closure
    # and setting its _db_initialized closure variable to False.
    tool_func = mcp_app[0]["jules_request_coding_task"]
    for i, var_name in enumerate(tool_func.__code__.co_freevars):
        if var_name == '_get_db':
            get_db_func = tool_func.__closure__[i].cell_contents
            for j, inner_var in enumerate(get_db_func.__code__.co_freevars):
                if inner_var == '_db_initialized':
                    get_db_func.__closure__[j].cell_contents = False
            break

    result = jules_request_coding_task("my-repo", "Refactor module X")
    assert result["status"] == "ERROR"
    assert "Failed to initialize Jules database" in result["error"]

    config.jules_db_path = original_path

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
def test_request_coding_task_api_error_and_sanitization(mock_urlopen, mcp_app):
    tools, config = mcp_app
    jules_request_coding_task = tools["jules_request_coding_task"]

    # First fetch succeeds to resolve source
    mock_sources_resp = MagicMock()
    mock_sources_resp.read.return_value = json.dumps({
        "sources": [{"name": "sources/github/owner/my-repo", "id": "github/owner/my-repo"}]
    }).encode("utf-8")

    # Mock HTTP error on the POST call leaking API key
    error_body = f"Server crashed with key {config.jules_api_key}".encode("utf-8")
    error = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
    error.read = MagicMock(return_value=error_body)

    mock_urlopen.side_effect = [
        MagicMock(__enter__=lambda _: mock_sources_resp, __exit__=lambda *a: None),
        error
    ]

    result = jules_request_coding_task("my-repo", "Refactor module X")

    assert result["status"] == "FALLIDO"
    assert "task_id" in result
    assert "HTTP error 401" in result["error"]
    assert "***REDACTED***" in result["error"]
    assert config.jules_api_key not in result["error"]

    # Verify DB state is FALLIDO for confirmed API errors
    with sqlite3.connect(config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM jules_jobs WHERE id = ?", (result["task_id"],))
        row = cursor.fetchone()
        assert row[0] == "FALLIDO"

@patch("urllib.request.urlopen")
def test_request_coding_task_uncertain_outcome(mock_urlopen, mcp_app):
    tools, config = mcp_app
    jules_request_coding_task = tools["jules_request_coding_task"]

    # First fetch succeeds to resolve source
    mock_sources_resp = MagicMock()
    mock_sources_resp.read.return_value = json.dumps({
        "sources": [{"name": "sources/github/owner/my-repo", "id": "github/owner/my-repo"}]
    }).encode("utf-8")

    # Mock a timeout error on the POST call
    import urllib.error
    timeout_err = urllib.error.URLError("Connection timed out")

    mock_urlopen.side_effect = [
        MagicMock(__enter__=lambda _: mock_sources_resp, __exit__=lambda *a: None),
        timeout_err
    ]

    result = jules_request_coding_task("my-repo", "Refactor module X")

    assert result["status"] == "DESCONOCIDO"
    assert "Connection timed out" in result["error"]

    # Verify DB state is DESCONOCIDO for uncertain outcomes
    with sqlite3.connect(config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM jules_jobs WHERE id = ?", (result["task_id"],))
        row = cursor.fetchone()
        assert row[0] == "DESCONOCIDO"

@patch("urllib.request.urlopen")
def test_ambiguous_repo_resolution(mock_urlopen, mcp_app):
    tools, config = mcp_app
    jules_request_coding_task = tools["jules_request_coding_task"]

    mock_sources_resp = MagicMock()
    mock_sources_resp.read.return_value = json.dumps({
        "sources": [
            {"name": "sources/github/owner1/my-repo", "id": "github/owner1/my-repo"},
            {"name": "sources/github/owner2/my-repo", "id": "github/owner2/my-repo"}
        ]
    }).encode("utf-8")

    mock_urlopen.side_effect = [
        MagicMock(__enter__=lambda _: mock_sources_resp, __exit__=lambda *a: None)
    ]

    result = jules_request_coding_task("my-repo", "Refactor")

    assert result["status"] == "FALLIDO"
    assert "matches multiple sources" in result["error"]


def test_check_task_status(mcp_app):
    tools, config = mcp_app
    jules_check_task_status = tools["jules_check_task_status"]

    # Init db properly first since we are using lazy init
    from dari_mcp_vps.tools.jules_db import init_db
    init_db(config.jules_db_path)

    # Insert mock data manually
    with sqlite3.connect(config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO jules_jobs (id, repo_name, task_description, jules_agent_job_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("mock-task", "test-repo", "test task", "sessions/test", "EN_PROGRESO", "now", "now"))
        conn.commit()

    # Add a mock task missing remote_state to test migration logic compatibility
    with sqlite3.connect(config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("ALTER TABLE jules_jobs DROP COLUMN remote_state")
        conn.commit()

    # Re-init should gracefully recreate the remote_state column
    tool_func = tools["jules_check_task_status"]
    for i, var_name in enumerate(tool_func.__code__.co_freevars):
        if var_name == '_get_db':
            get_db_func = tool_func.__closure__[i].cell_contents
            for j, inner_var in enumerate(get_db_func.__code__.co_freevars):
                if inner_var == '_db_initialized':
                    get_db_func.__closure__[j].cell_contents = False
            break

    with sqlite3.connect(config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE jules_jobs SET status = 'EN_PROGRESO' WHERE id = 'mock-task'
        """)
        conn.commit()

    result = jules_check_task_status("mock-task")

    assert result["task_id"] == "mock-task"
    assert result["repo_name"] == "test-repo"
    assert result["task_description"] == "test task"
    assert result["jules_agent_job_id"] == "sessions/test"
    assert result["status"] == "EN_PROGRESO"
    assert result["is_local_cache"] is True
    assert result["remote_state"] is None

    not_found = jules_check_task_status("invalid-id")
    assert not_found["status"] == "NOT_FOUND"
