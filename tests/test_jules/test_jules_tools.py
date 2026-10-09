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
    error = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)

    mock_urlopen.side_effect = [
        MagicMock(__enter__=lambda _: mock_sources_resp, __exit__=lambda *a: None),
        error
    ]

    result = jules_request_coding_task("my-repo", "Refactor module X")

    assert result["status"] == "FALLIDO"
    assert "task_id" in result
    assert "API rejected session creation (HTTP 401)" in result["error"]

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
    assert "Failed to contact API or connection timed out during submission" in result["error"]

    # Verify DB state is DESCONOCIDO for uncertain outcomes
    with sqlite3.connect(config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM jules_jobs WHERE id = ?", (result["task_id"],))
        row = cursor.fetchone()
        assert row[0] == "DESCONOCIDO"

@patch("urllib.request.urlopen")
def test_request_coding_task_get_timeout(mock_urlopen, mcp_app):
    tools, config = mcp_app
    jules_request_coding_task = tools["jules_request_coding_task"]

    # Mock a timeout error on the GET call
    import urllib.error
    timeout_err = urllib.error.URLError("Connection timed out")

    mock_urlopen.side_effect = [timeout_err]

    result = jules_request_coding_task("my-repo", "Refactor module X")

    assert result["status"] == "FALLIDO"
    assert "Failed to resolve repository (network error or timeout)" in result["error"]

    # Verify DB state is FALLIDO for GET timeouts
    with sqlite3.connect(config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM jules_jobs WHERE id = ?", (result["task_id"],))
        row = cursor.fetchone()
        assert row[0] == "FALLIDO"

@patch("urllib.request.urlopen")
def test_request_coding_task_post_persistence_failure(mock_urlopen, mcp_app):
    tools, config = mcp_app
    jules_request_coding_task = tools["jules_request_coding_task"]

    mock_sources_resp = MagicMock()
    mock_sources_resp.read.return_value = json.dumps({
        "sources": [{"name": "sources/github/owner/my-repo", "id": "github/owner/my-repo"}]
    }).encode("utf-8")

    mock_sessions_resp = MagicMock()
    mock_sessions_resp.read.return_value = json.dumps({
        "name": "sessions/12345",
        "id": "12345",
        "state": "QUEUED"
    }).encode("utf-8")

    mock_urlopen.side_effect = [
        MagicMock(__enter__=lambda _: mock_sources_resp, __exit__=lambda *a: None),
        MagicMock(__enter__=lambda _: mock_sessions_resp, __exit__=lambda *a: None)
    ]

    with patch("dari_mcp_vps.tools.jules_tools.update_job_remote_id", side_effect=Exception("DB Error")):
        result = jules_request_coding_task("my-repo", "Refactor module X")

    assert result["status"] == "EN_PROGRESO"
    assert result["jules_agent_job_id"] == "sessions/12345"
    assert "Failed to persist task locally" in result["error"]

@patch("urllib.request.urlopen")
def test_request_coding_task_post_500(mock_urlopen, mcp_app):
    tools, config = mcp_app
    jules_request_coding_task = tools["jules_request_coding_task"]

    mock_sources_resp = MagicMock()
    mock_sources_resp.read.return_value = json.dumps({
        "sources": [{"name": "sources/github/owner/my-repo", "id": "github/owner/my-repo"}]
    }).encode("utf-8")

    error = urllib.error.HTTPError("url", 500, "Internal Server Error", {}, None)

    mock_urlopen.side_effect = [
        MagicMock(__enter__=lambda _: mock_sources_resp, __exit__=lambda *a: None),
        error
    ]

    result = jules_request_coding_task("my-repo", "Refactor module X")

    assert result["status"] == "DESCONOCIDO"
    assert "Failed to contact API or connection timed out during submission (HTTP 500)" in result["error"]

    with sqlite3.connect(config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM jules_jobs WHERE id = ?", (result["task_id"],))
        row = cursor.fetchone()
        assert row[0] == "DESCONOCIDO"

@patch("urllib.request.urlopen")
def test_request_coding_task_persistence_queued(mock_urlopen, mcp_app):
    tools, config = mcp_app
    jules_request_coding_task = tools["jules_request_coding_task"]

    mock_sources_resp = MagicMock()
    mock_sources_resp.read.return_value = json.dumps({
        "sources": [{"name": "sources/github/owner/my-repo", "id": "github/owner/my-repo"}]
    }).encode("utf-8")

    mock_sessions_resp = MagicMock()
    mock_sessions_resp.read.return_value = json.dumps({
        "name": "sessions/12345",
        "id": "12345",
        "state": "QUEUED"
    }).encode("utf-8")

    mock_urlopen.side_effect = [
        MagicMock(__enter__=lambda _: mock_sources_resp, __exit__=lambda *a: None),
        MagicMock(__enter__=lambda _: mock_sessions_resp, __exit__=lambda *a: None)
    ]

    result = jules_request_coding_task("my-repo", "Refactor module X")

    assert result["status"] == "EN_PROGRESO"
    assert result["jules_agent_job_id"] == "sessions/12345"

    with sqlite3.connect(config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT remote_state FROM jules_jobs WHERE id = ?", (result["task_id"],))
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "QUEUED"

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

@patch("urllib.request.urlopen")
def test_jules_get_task_activities_list_mode(mock_urlopen, mcp_app):
    tools, config = mcp_app
    jules_get_task_activities = tools["jules_get_task_activities"]

    from dari_mcp_vps.tools.jules_db import init_db
    init_db(config.jules_db_path)

    import sqlite3
    with sqlite3.connect(config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO jules_jobs (id, repo_name, task_description, jules_agent_job_id, status, created_at, updated_at) VALUES ('t1', 'repo', 'desc', 'session1', 'PENDING', '2023', '2023')")
        conn.commit()

    mock_resp = MagicMock()
    # Missing activityType to test derivation, and secret key in text to test sanitation before truncation
    long_msg = "X" * 1500 + config.jules_api_key + "Y" * 10
    mock_resp.read.return_value = json.dumps({
        "activities": [
            {
                "id": "act1",
                "createTime": "2023",
                "agentMessaged": {"agentMessage": long_msg}
            }
        ],
        "nextPageToken": "token2"
    }).encode("utf-8")

    mock_urlopen.return_value.__enter__.return_value = mock_resp

    result = jules_get_task_activities("t1")
    assert "error" not in result
    assert len(result["activities"]) == 1
    act = result["activities"][0]

    # Derived type
    assert act["activityType"] == "AGENT_MESSAGED"

    # Truncated
    assert act.get("is_truncated") is True
    msg = act["agentMessage"]
    assert len(msg) <= 1003 # 1000 + "..."
    assert config.jules_api_key not in msg

@patch("urllib.request.urlopen")
def test_jules_get_task_activities_detail_mode(mock_urlopen, mcp_app):
    tools, config = mcp_app
    jules_get_task_activities = tools["jules_get_task_activities"]

    from dari_mcp_vps.tools.jules_db import init_db
    init_db(config.jules_db_path)

    import sqlite3
    with sqlite3.connect(config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO jules_jobs (id, repo_name, task_description, jules_agent_job_id, status, created_at, updated_at) VALUES ('t1', 'repo', 'desc', 'session1', 'PENDING', '2023', '2023')")
        conn.commit()

    # The content we want to chunk, including a secret
    long_content = "A" * 8000 + "B" * 4000 + config.jules_api_key + "C" * 100
    expected_redacted = "A" * 8000 + "B" * 4000 + "***REDACTED***" + "C" * 100

    mock_resp1 = MagicMock()
    mock_resp1.read.return_value = json.dumps({
        "activities": [
            {"id": "act_other"}
        ],
        "nextPageToken": "token2"
    }).encode("utf-8")

    mock_resp2 = MagicMock()
    mock_resp2.read.return_value = json.dumps({
        "activities": [
            {
                "id": "act_target",
                "agentMessaged": {"agentMessage": long_content}
            }
        ]
    }).encode("utf-8")

    mock_urlopen.side_effect = [
        MagicMock(__enter__=lambda _: mock_resp1, __exit__=lambda *a: None),
        MagicMock(__enter__=lambda _: mock_resp2, __exit__=lambda *a: None)
    ]

    # First fetch (offset 0)
    res1 = jules_get_task_activities("t1", activity_id="act_target", content_offset=0)
    assert res1["activity_id"] == "act_target"
    assert len(res1["fragment"]) == 8000
    assert res1["fragment"] == expected_redacted[0:8000]
    assert res1["has_more"] is True
    assert res1["next_content_offset"] == 8000
    assert res1["total_length"] == len(expected_redacted)

    # Reset mocks for next offset call
    mock_urlopen.side_effect = [
        MagicMock(__enter__=lambda _: mock_resp1, __exit__=lambda *a: None),
        MagicMock(__enter__=lambda _: mock_resp2, __exit__=lambda *a: None)
    ]

    res2 = jules_get_task_activities("t1", activity_id="act_target", content_offset=8000)
    assert res2["fragment"] == expected_redacted[8000:16000]
    assert res2["has_more"] is False
    assert res2["next_content_offset"] is None
    assert "***REDACTED***" in res2["fragment"]
    assert config.jules_api_key not in res2["fragment"]

def test_job_observation_persistence(mcp_app):
    tools, config = mcp_app
    jules_check_task_status = tools["jules_check_task_status"]

    from dari_mcp_vps.tools.jules_db import init_db, record_job_observation
    init_db(config.jules_db_path)

    import sqlite3
    with sqlite3.connect(config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO jules_jobs (id, repo_name, task_description, status, created_at, updated_at) VALUES ('obs1', 'repo', 'desc', 'PENDING', '2023', '2023')")
        conn.commit()

    record_job_observation(config.jules_db_path, "obs1", success=True)
    st = jules_check_task_status("obs1")
    assert st["remote_observed_at"] is not None
    assert st["remote_observation_error"] is None

    record_job_observation(config.jules_db_path, "obs1", success=False, error_msg="fail")
    st2 = jules_check_task_status("obs1")
    assert st2["remote_observation_error"] == "fail"
    # remote_observed_at should remain intact (last known success)
    assert st2["remote_observed_at"] == st["remote_observed_at"]

@patch("urllib.request.urlopen")
def test_jules_get_task_activities_unsupported_type(mock_urlopen, mcp_app):
    tools, config = mcp_app
    jules_get_task_activities = tools["jules_get_task_activities"]

    from dari_mcp_vps.tools.jules_db import init_db
    init_db(config.jules_db_path)
    import sqlite3
    with sqlite3.connect(config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO jules_jobs (id, repo_name, task_description, jules_agent_job_id, status, created_at, updated_at) VALUES ('t_unsup', 'repo', 'desc', 'session1', 'PENDING', '2023', '2023')")
        conn.commit()

    mock_resp = MagicMock()
    # A progress update with artifacts/patch
    mock_resp.read.return_value = json.dumps({
        "activities": [
            {
                "id": "act_patch",
                "activityType": "PROGRESS_UPDATED",
                "progressUpdated": {
                    "changeSet": {"gitPatch": "diff --git a/file.txt b/file.txt..."}
                }
            }
        ]
    }).encode("utf-8")

    mock_urlopen.return_value.__enter__.return_value = mock_resp

    res = jules_get_task_activities("t_unsup", activity_id="act_patch", content_offset=0)
    assert res["fragment"] == "Unsupported activity type: PROGRESS_UPDATED"
    assert res["has_more"] is False

@patch("urllib.request.urlopen")
def test_jules_get_task_activities_invalid_offset(mock_urlopen, mcp_app):
    tools, config = mcp_app
    jules_get_task_activities = tools["jules_get_task_activities"]

    from dari_mcp_vps.tools.jules_db import init_db
    init_db(config.jules_db_path)
    import sqlite3
    with sqlite3.connect(config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO jules_jobs (id, repo_name, task_description, jules_agent_job_id, status, created_at, updated_at) VALUES ('t_off', 'repo', 'desc', 'session1', 'PENDING', '2023', '2023')")
        conn.commit()

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({
        "activities": [
            {
                "id": "act_msg",
                "agentMessaged": {"agentMessage": "Hello"}
            }
        ]
    }).encode("utf-8")

    mock_urlopen.return_value.__enter__.return_value = mock_resp

    res1 = jules_get_task_activities("t_off", activity_id="act_msg", content_offset=-1)
    assert "error" in res1
    assert "cannot be negative" in res1["error"]

    res2 = jules_get_task_activities("t_off", activity_id="act_msg", content_offset=10)
    assert res2["fragment"] == ""
    assert res2["has_more"] is False
    assert res2["next_content_offset"] is None
