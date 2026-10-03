import json
import sqlite3
import urllib.error
from unittest.mock import patch, MagicMock
import tempfile
import os
import pytest
import asyncio
from unittest.mock import AsyncMock

from dari_mcp_vps.config import AppConfig
from dari_mcp_vps.tools.jules_db import init_db, create_job, update_job_remote_id, get_pending_events
from dari_mcp_vps.tools.jules_monitor import background_monitor

@pytest.fixture
def mock_config():
    with tempfile.NamedTemporaryFile(delete=False) as tmp_db:
        db_path = tmp_db.name

    class MockAppConfig(AppConfig):
        def __init__(self):
            super().__init__({"jules": {"api_key": "test_key", "api_url": "https://test.jules.api", "n8n_webhook_url": "http://n8n.webhook", "n8n_webhook_key": "secret123"}}, "")
            self._key = "test_key"
            self._db_path = db_path
            self._n8n_url = "http://n8n.webhook"
            self._n8n_key = "secret123"

        @property
        def jules_api_key(self):
            return self._key

        @property
        def jules_api_url(self):
            return "https://test.jules.api"

        @property
        def jules_db_path(self):
            return self._db_path

        @property
        def n8n_webhook_url(self):
            return self._n8n_url

        @property
        def n8n_webhook_key(self):
            return self._n8n_key

    config = MockAppConfig()
    init_db(config.jules_db_path)
    yield config
    if os.path.exists(config.jules_db_path):
        os.remove(config.jules_db_path)

@patch("asyncio.sleep", new_callable=AsyncMock)
@pytest.mark.asyncio
async def test_monitor_state_transition_and_webhook(mock_sleep, mock_config):
    job_id = create_job(mock_config.jules_db_path, "my-repo", "Task 1")
    update_job_remote_id(mock_config.jules_db_path, job_id, "sessions/123")

    jules_resp = MagicMock()
    jules_resp.read.return_value = json.dumps({"state": "AWAITING_PLAN_APPROVAL"}).encode("utf-8")
    jules_resp.status = 200

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = jules_resp

        # We need to mock sleep to run the loop exactly once
        mock_sleep.side_effect = [None, Exception("Stop loop")]
        try:
            await background_monitor(mock_config)
        except Exception as e:
            if str(e) != "Stop loop":
                raise

    with sqlite3.connect(mock_config.jules_db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM jules_jobs WHERE id = ?", (job_id,))
        job = dict(cursor.fetchone())

        assert job["status"] == "ESPERANDO_FEEDBACK"
        assert job["remote_state"] == "AWAITING_PLAN_APPROVAL"

        cursor.execute("SELECT * FROM jules_events WHERE job_id = ?", (job_id,))
        events = cursor.fetchall()
        assert len(events) == 1
        assert dict(events[0])["status"] == "SENT" # it gets sent immediately because mock_sleep=[None, ...] means loop runs twice

        payload = json.loads(dict(events[0])["payload"])
        assert payload["event_type"] == "STATE_CHANGED"
        assert payload["remote_state"] == "AWAITING_PLAN_APPROVAL"
        assert "event_id" in payload
        assert "timestamp" in payload

@patch("asyncio.sleep", new_callable=AsyncMock)
@pytest.mark.asyncio
async def test_monitor_completed_with_pr_and_webhook_retry(mock_sleep, mock_config):
    job_id = create_job(mock_config.jules_db_path, "my-repo", "Task 1")
    update_job_remote_id(mock_config.jules_db_path, job_id, "sessions/123")

    jules_resp = MagicMock()
    jules_resp.read.return_value = json.dumps({
        "state": "COMPLETED",
        "outputs": [{"pullRequest": {"url": "https://github.com/my-repo/pull/1", "title": "My PR"}}]
    }).encode("utf-8")
    jules_resp.status = 200

    webhook_resp = MagicMock()
    webhook_resp.status = 200

    def side_effect_func(*args, **kwargs):
        req = args[0]
        if hasattr(req, "full_url") and "n8n" in req.full_url:
            return MagicMock(__enter__=lambda _: webhook_resp, __exit__=lambda *a: None)
        else:
            return MagicMock(__enter__=lambda _: jules_resp, __exit__=lambda *a: None)

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = side_effect_func

        mock_sleep.side_effect = [None, None, Exception("Stop loop")]
        try:
            await background_monitor(mock_config)
        except Exception as e:
            if str(e) != "Stop loop":
                raise

    post_req = None
    for call in mock_urlopen.call_args_list:
        req = call[0][0]
        if hasattr(req, "full_url") and "n8n" in req.full_url:
            post_req = req
            break

    assert post_req is not None
    assert post_req.get_header("X-jules-webhook-key") == "secret123"

    with sqlite3.connect(mock_config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM jules_jobs WHERE id = ?", (job_id,))
        assert cursor.fetchone()[0] == "COMPLETED"

        cursor.execute("SELECT status FROM jules_events WHERE job_id = ?", (job_id,))
        assert cursor.fetchone()[0] == "SENT"
