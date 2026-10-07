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

    activities_resp = MagicMock()
    activities_resp.read.return_value = json.dumps({"activities": []}).encode("utf-8")
    activities_resp.status = 200

    def urlopen_side_effect(req, *args, **kwargs):
        if hasattr(req, "full_url") and "activities" in req.full_url:
            return MagicMock(__enter__=lambda _: activities_resp, __exit__=lambda *a: None)
        return MagicMock(__enter__=lambda _: jules_resp, __exit__=lambda *a: None)

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urlopen_side_effect

        # We need to mock sleep to run the loop twice so the webhook dispatch loop catches it
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
        assert dict(events[0])["status"] == "SENT"

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
        "outputs": [{"pullRequest": {"http://url": "https://github.com/my-repo/pull/1", "title": "My PR"}}]
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

    # Mock both the session call and the activities call
    activities_resp = MagicMock()
    activities_resp.read.return_value = json.dumps({"activities": []}).encode("utf-8")
    activities_resp.status = 200

    def side_effect_func(*args, **kwargs):
        req = args[0]
        if hasattr(req, "full_url") and "n8n" in req.full_url:
            return MagicMock(__enter__=lambda _: webhook_resp, __exit__=lambda *a: None)
        elif hasattr(req, "full_url") and "activities" in req.full_url:
            return MagicMock(__enter__=lambda _: activities_resp, __exit__=lambda *a: None)
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

@patch("asyncio.sleep", new_callable=AsyncMock)
@pytest.mark.asyncio
async def test_monitor_resume_after_feedback_and_restart(mock_sleep, mock_config):
    job_id = create_job(mock_config.jules_db_path, "my-repo", "Task 1")
    update_job_remote_id(mock_config.jules_db_path, job_id, "sessions/123")

    # Directly set to ESPERANDO_FEEDBACK simulating a restart state
    with sqlite3.connect(mock_config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE jules_jobs
            SET status = 'ESPERANDO_FEEDBACK', remote_state = 'AWAITING_PLAN_APPROVAL'
            WHERE id = ?
        """, (job_id,))
        conn.commit()

    jules_resp_in_progress = MagicMock()
    jules_resp_in_progress.read.return_value = json.dumps({"state": "IN_PROGRESS"}).encode("utf-8")
    jules_resp_in_progress.status = 200

    jules_resp_completed = MagicMock()
    jules_resp_completed.read.return_value = json.dumps({
        "state": "COMPLETED",
        "outputs": [{"pullRequest": {"http://url": "https://github.com/my-repo/pull/1", "title": "My PR"}}]
    }).encode("utf-8")
    jules_resp_completed.status = 200

    webhook_resp = MagicMock()
    webhook_resp.status = 200

    call_count = 0
    def side_effect_func(req, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if hasattr(req, "full_url") and "n8n" in req.full_url:
            return MagicMock(__enter__=lambda _: webhook_resp, __exit__=lambda *a: None)
        else:
            # First polling returns IN_PROGRESS, second returns COMPLETED
            # Note: with webhook deliveries, there will be more calls.
            # We just track how many times Jules API is called to sequence it.
            if call_count <= 2:
                # E.g. Jules API poll 1 -> IN_PROGRESS
                # Actually, call_count tracks ALL urllib calls.
                return MagicMock(__enter__=lambda _: jules_resp_in_progress, __exit__=lambda *a: None)
            else:
                return MagicMock(__enter__=lambda _: jules_resp_completed, __exit__=lambda *a: None)

    activities_resp = MagicMock()
    activities_resp.read.return_value = json.dumps({"activities": []}).encode("utf-8")
    activities_resp.status = 200

    jules_call_count = 0
    def jules_side_effect(req, *args, **kwargs):
        nonlocal jules_call_count
        if hasattr(req, "full_url") and "n8n" in req.full_url:
            return MagicMock(__enter__=lambda _: webhook_resp, __exit__=lambda *a: None)
        elif hasattr(req, "full_url") and "activities" in req.full_url:
            return MagicMock(__enter__=lambda _: activities_resp, __exit__=lambda *a: None)
        else:
            jules_call_count += 1
            if jules_call_count == 1:
                return MagicMock(__enter__=lambda _: jules_resp_in_progress, __exit__=lambda *a: None)
            else:
                return MagicMock(__enter__=lambda _: jules_resp_completed, __exit__=lambda *a: None)

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = jules_side_effect

        # Let it run enough times to transition to IN_PROGRESS, send webhook, transition to COMPLETED, send webhook.
        mock_sleep.side_effect = [None, None, None, None, Exception("Stop loop")]
        try:
            await background_monitor(mock_config)
        except Exception as e:
            if str(e) != "Stop loop":
                raise

    with sqlite3.connect(mock_config.jules_db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Final status should be COMPLETED
        cursor.execute("SELECT status, remote_state FROM jules_jobs WHERE id = ?", (job_id,))
        job = dict(cursor.fetchone())
        assert job["status"] == "COMPLETED"
        assert job["remote_state"] == "COMPLETED"

        # We should have 2 events: one for IN_PROGRESS and one for COMPLETED
        cursor.execute("SELECT status, payload FROM jules_events WHERE job_id = ? ORDER BY created_at ASC", (job_id,))
        events = cursor.fetchall()
        assert len(events) == 2

        event_1 = json.loads(dict(events[0])["payload"])
        assert event_1["remote_state"] == "IN_PROGRESS"

        event_2 = json.loads(dict(events[1])["payload"])
        assert event_2["remote_state"] == "COMPLETED"

@patch("asyncio.sleep", new_callable=AsyncMock)
@pytest.mark.asyncio
async def test_monitor_agent_messaged_activity(mock_sleep, mock_config):
    job_id = create_job(mock_config.jules_db_path, "my-repo", "Task 1")
    update_job_remote_id(mock_config.jules_db_path, job_id, "sessions/123")

    # Simulating a state that hasn't changed but with new activities
    jules_resp = MagicMock()
    jules_resp.read.return_value = json.dumps({"state": "AWAITING_USER_FEEDBACK"}).encode("utf-8")
    jules_resp.status = 200

    import datetime
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

    activities_resp = MagicMock()
    activities_resp.read.return_value = json.dumps({
        "activities": [
            {
                "id": "act-1",
                "activityType": "AGENT_MESSAGED",
                "createTime": now_str,
                "agentMessaged": {"agentMessage": "Please clarify."}
            }
        ]
    }).encode("utf-8")
    activities_resp.status = 200

    def urlopen_side_effect(req, *args, **kwargs):
        if hasattr(req, "full_url") and "activities" in req.full_url:
            return MagicMock(__enter__=lambda _: activities_resp, __exit__=lambda *a: None)
        return MagicMock(__enter__=lambda _: jules_resp, __exit__=lambda *a: None)

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urlopen_side_effect

        mock_sleep.side_effect = [None, Exception("Stop loop")]
        try:
            await background_monitor(mock_config)
        except Exception as e:
            if str(e) != "Stop loop":
                raise

    with sqlite3.connect(mock_config.jules_db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # We should have an event for STATE_CHANGED and an event for AGENT_MESSAGE
        # actually, because we don't have remote_state yet locally, it will emit a STATE_CHANGED
        # AND it will emit an AGENT_MESSAGE
        cursor.execute("SELECT payload, event_type FROM jules_events WHERE job_id = ? ORDER BY created_at ASC", (job_id,))
        events = cursor.fetchall()

        agent_messages = [json.loads(e["payload"]) for e in events if e["event_type"] == "AGENT_MESSAGE"]
        assert len(agent_messages) == 1
        assert agent_messages[0]["context"] == "Please clarify."
        assert agent_messages[0]["activity_id"] == "act-1"
        assert "remote_state" not in agent_messages[0]

@patch("asyncio.sleep", new_callable=AsyncMock)
@pytest.mark.asyncio
async def test_monitor_dedup_and_pagination(mock_sleep, mock_config):
    job_id = create_job(mock_config.jules_db_path, "my-repo", "Task 1")
    update_job_remote_id(mock_config.jules_db_path, job_id, "sessions/123")

    # 1. State unchanged, just processing activities with > 5 pages
    jules_resp = MagicMock()
    jules_resp.read.return_value = json.dumps({"state": "AWAITING_USER_FEEDBACK"}).encode("utf-8")
    jules_resp.status = 200

    import datetime
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def make_page(token, next_token):
        m = MagicMock()
        m.status = 200
        m.read.return_value = json.dumps({
            "activities": [{
                "id": f"act-{token}",
                "activityType": "AGENT_MESSAGED",
                "createTime": now_str,
                "agentMessaged": {"agentMessage": f"Msg {token}"}
            }],
            "nextPageToken": next_token
        }).encode("utf-8")
        return m

    pages = {
        None: make_page("1", "page2"),
        "page2": make_page("2", "page3"),
        "page3": make_page("3", "page4"),
        "page4": make_page("4", "page5"),
        "page5": make_page("5", "page6"),
        "page6": make_page("6", None) # this shouldn't be reached in first cycle
    }

    def urlopen_side_effect(req, *args, **kwargs):
        if hasattr(req, "full_url") and "activities" in req.full_url:
            import urllib.parse
            parsed = urllib.parse.urlparse(req.full_url)
            qs = urllib.parse.parse_qs(parsed.query)
            # handle case where token is None in qs
            token = qs.get("pageToken", [None])[0] if "pageToken" in qs else None
            return MagicMock(__enter__=lambda _: pages[token], __exit__=lambda *a: None)
        return MagicMock(__enter__=lambda _: jules_resp, __exit__=lambda *a: None)

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urlopen_side_effect
        mock_sleep.side_effect = [Exception("Stop loop")]
        try:
            await background_monitor(mock_config)
        except Exception as e:
            if str(e) != "Stop loop":
                raise

    with sqlite3.connect(mock_config.jules_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT activities_cursor FROM jules_jobs WHERE id = ?", (job_id,))
        cursor_val = cursor.fetchone()[0]
        assert cursor_val == "page6" # Saved where it stopped (after 5 pages)

        cursor.execute("SELECT count(*) FROM jules_events WHERE event_type = 'AGENT_MESSAGE'")
        count = cursor.fetchone()[0]
        assert count == 5

        # Test dedup: run it again, it should resume from page6
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urlopen_side_effect
            mock_sleep.side_effect = [Exception("Stop loop")]
            try:
                await background_monitor(mock_config)
            except Exception as e:
                if str(e) != "Stop loop":
                    raise

        cursor.execute("SELECT activities_cursor FROM jules_jobs WHERE id = ?", (job_id,))
        assert cursor.fetchone()[0] is None # Reached end

        cursor.execute("SELECT count(*) FROM jules_events WHERE event_type = 'AGENT_MESSAGE'")
        count = cursor.fetchone()[0]
        assert count == 6 # Added the 6th

        # Third run: no new events, cursor remains None, dedup prevents re-inserting
        # Since it starts from None, it hits page1 but dedup ignores it
        pages[None] = make_page("1", None) # simulate only 1 page now
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urlopen_side_effect
            mock_sleep.side_effect = [Exception("Stop loop")]
            try:
                await background_monitor(mock_config)
            except Exception as e:
                if str(e) != "Stop loop":
                    raise

        cursor.execute("SELECT count(*) FROM jules_events WHERE event_type = 'AGENT_MESSAGE'")
        count = cursor.fetchone()[0]
        assert count == 6 # Still 6

@patch("urllib.request.urlopen")
@pytest.mark.asyncio
async def test_state_fidelity_on_activities_failure(mock_urlopen):
    db_path = "test_fidelity.db"
    import os
    if os.path.exists(db_path):
        os.remove(db_path)

    from dari_mcp_vps.tools.jules_db import init_db, create_job, update_job_remote_id, update_job_status
    init_db(db_path)
    job_id = create_job(db_path, "test/repo", "desc")
    update_job_remote_id(db_path, job_id, "sessions/123")

    from dari_mcp_vps.config import AppConfig
    class MockConfig(AppConfig):
        def __init__(self):
            super().__init__({"jules": {"api_key": "sec", "api_url": "http://url"}}, "")
            self._db_path = db_path

        @property
        def jules_api_key(self):
            return "sec"
        @property
        def jules_db_path(self):
            return self._db_path

    config = MockConfig()

    import urllib.error
    # Mock first call (session) to succeed, second call (activities) to return 404
    mock_session_resp = MagicMock()
    mock_session_resp.read.return_value = json.dumps({"state": "IN_PROGRESS"}).encode("utf-8")

    import io
    err_fp = io.BytesIO(b"Not Found")
    err_404 = urllib.error.HTTPError("http://url", 404, "Not Found", {}, err_fp)

    mock_urlopen.side_effect = [
        MagicMock(__enter__=lambda _: mock_session_resp, __exit__=lambda *a: None),
        err_404
    ]

    from dari_mcp_vps.tools.jules_monitor import background_monitor
    import asyncio

    task = asyncio.create_task(background_monitor(config))
    await asyncio.sleep(0.1) # Let it run one loop
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass

    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM jules_jobs WHERE id = ?", (job_id,))
        job = dict(cursor.fetchone())

    # State should remain EN_PROGRESO (derived from IN_PROGRESS) and remote_observed_at should be populated
    assert job["status"] == "EN_PROGRESO"
    assert job["remote_observed_at"] is not None
    assert job["remote_observation_error"] is None # We consider it a success because session was read

@patch("urllib.request.urlopen")
@pytest.mark.asyncio
async def test_session_get_failure_updates_fidelity(mock_urlopen):
    db_path = "test_fidelity_2.db"
    import os
    if os.path.exists(db_path):
        os.remove(db_path)

    from dari_mcp_vps.tools.jules_db import init_db, create_job, update_job_remote_id
    init_db(db_path)
    job_id = create_job(db_path, "test/repo", "desc")
    update_job_remote_id(db_path, job_id, "sessions/123")

    from dari_mcp_vps.config import AppConfig
    class MockConfig(AppConfig):
        def __init__(self):
            super().__init__({"jules": {"api_key": "sec", "api_url": "http://url"}}, "")
            self._db_path = db_path
        @property
        def jules_api_key(self):
            return "sec"
        @property
        def jules_db_path(self):
            return self._db_path

    config = MockConfig()

    import urllib.error
    import io
    err_fp = io.BytesIO(b"Not Found")
    err_404 = urllib.error.HTTPError("http://url", 404, "Not Found", {}, err_fp)
    mock_urlopen.side_effect = err_404

    from dari_mcp_vps.tools.jules_monitor import background_monitor
    import asyncio

    task = asyncio.create_task(background_monitor(config))
    await asyncio.sleep(0.1) # Let it run one loop
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass

    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM jules_jobs WHERE id = ?", (job_id,))
        job = dict(cursor.fetchone())

    # State should become FALLIDO, remote_state NOT_FOUND
    assert job["status"] == "FALLIDO"
    assert job["remote_state"] == "NOT_FOUND"
    assert job["remote_observation_error"] is not None

@patch("urllib.request.urlopen")
@pytest.mark.asyncio
async def test_monitor_logger_security(mock_urlopen, caplog):
    db_path = "test_sec.db"
    import os
    if os.path.exists(db_path):
        os.remove(db_path)

    from dari_mcp_vps.tools.jules_db import init_db, create_job, update_job_remote_id
    init_db(db_path)
    job_id = create_job(db_path, "test/repo", "desc")
    update_job_remote_id(db_path, job_id, "sessions/123")

    from dari_mcp_vps.config import AppConfig
    class MockConfig(AppConfig):
        def __init__(self):
            super().__init__({"jules": {"api_key": "mysecretkey123", "api_url": "http://url"}}, "")
            self._db_path = db_path
        @property
        def jules_api_key(self):
            return "mysecretkey123"
        @property
        def jules_db_path(self):
            return self._db_path

    config = MockConfig()

    # Force a RuntimeError with the secret key in the message
    mock_urlopen.side_effect = RuntimeError("Failed with key mysecretkey123 something else")

    from dari_mcp_vps.tools.jules_monitor import background_monitor
    import asyncio

    task = asyncio.create_task(background_monitor(config))
    await asyncio.sleep(0.1) # Let it run one loop
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass

    for record in caplog.records:
        assert "mysecretkey123" not in record.message
        if "Failed with key" in record.message:
            assert "***REDACTED***" in record.message
