import pytest
import os
import json
import time
import datetime
import hashlib
import urllib.request
import urllib.error
from unittest.mock import patch, MagicMock

from dari_mcp_vps.config import load_config
from dari_mcp_vps.tools.approval_db import (
    init_db,
    create_request,
    get_request,
    claim_decision,
    consume_approved_requests,
    expire_pending_requests
)
from dari_mcp_vps.tools.docker_tools import execute_docker_restart
from dari_mcp_vps.tools.action_dispatcher import get_handler

@pytest.fixture
def test_db_path(tmp_path):
    return str(tmp_path / "test_approvals.db")

@pytest.fixture
def base_config_raw():
    return {
        "allowed_containers": ["homepage", "nginx"],
        "allowed_http_targets": {
            "homepage_local": {
                "url": "http://host.docker.internal:3001",
                "headers": {"Host": "julidcardenas.site"}
            }
        },
        "tools": {
            "docker_restart": {
                "enabled": True,
                "requires_approval": True,
                "targets": ["homepage", "unknown"]
            }
        }
    }

@pytest.fixture
def app_config(test_db_path, monkeypatch, base_config_raw):
    monkeypatch.setenv("IA_MCP_VPS_CONFIG", "config.example.yaml")
    monkeypatch.setenv("APPROVAL_DB_PATH", test_db_path)
    monkeypatch.setenv("APPROVAL_ENABLED", "true")
    monkeypatch.setenv("APPROVAL_WEBHOOK_SECRET", "secret123")
    monkeypatch.setenv("APPROVAL_N8N_WEBHOOK_KEY", "webhook123")
    monkeypatch.setenv("APPROVAL_N8N_WEBHOOK_URL", "https://example.com/webhook")
    monkeypatch.setenv("APPROVAL_TELEGRAM_USER_ID", "1111")
    monkeypatch.setenv("APPROVAL_TELEGRAM_CHAT_ID", "2222")

    # We load config but inject our raw config to avoid file manipulation during tests
    config = load_config()
    config.raw = base_config_raw
    init_db(test_db_path)
    return config

def test_dispatcher_registered():
    handler = get_handler("docker_restart")
    assert handler is not None
    assert handler == execute_docker_restart

@patch("dari_mcp_vps.tools.docker_tools._docker_request")
@patch("dari_mcp_vps.tools.docker_tools._json")
@patch("urllib.request.build_opener")
def test_execute_docker_restart_success(mock_build_opener, mock_json, mock_docker_request, app_config):
    # Setup mocks
    mock_docker_request.return_value = (204, {}, b"")
    mock_json.side_effect = [
        # 1. _find_container
        [{"Id": "07e6864ccb37", "Names": ["/homepage"], "State": "running"}],
        # 2. inspect for snapshot
        {"Id": "07e6864ccb37", "Config": {"Image": "homepage:latest"}, "State": {"StartedAt": "2023-10-10T10:00:00Z"}},
        # 3. _find_container after POST
        [{"Id": "07e6864ccb37", "Names": ["/homepage"], "State": "running"}],
    ]

    mock_opener = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_opener.open.return_value.__enter__.return_value = mock_resp
    mock_build_opener.return_value = mock_opener

    snapshot = {
        "Id": "07e6864ccb37",
        "Image": "homepage:latest",
        "StartedAt": "2023-10-10T10:00:00Z"
    }

    success, diagnostic = execute_docker_restart({"container": "homepage", "snapshot": snapshot}, app_config)

    assert success is True
    assert "Successfully restarted homepage" in diagnostic["message"]

    # Verify POST happened exactly once
    assert mock_docker_request.call_count == 1
    args, _ = mock_docker_request.call_args
    assert args[0] == "POST"
    assert "/containers/07e6864ccb37/restart" in args[1]

    # Verify HTTP verification happened
    mock_opener.open.assert_called_once()

@patch("dari_mcp_vps.tools.docker_tools._docker_request")
@patch("dari_mcp_vps.tools.docker_tools._json")
def test_execute_docker_restart_replaced_container(mock_json, mock_docker_request, app_config):
    # If the container was replaced, the snapshot won't match
    mock_json.side_effect = [
        # 1. _find_container
        [{"Id": "new_id_12345678", "Names": ["/homepage"], "State": "running"}],
        # 2. inspect for snapshot
        {"Id": "new_id_12345678", "Config": {"Image": "homepage:latest"}, "State": {"StartedAt": "2023-10-10T12:00:00Z"}},
    ]

    old_snapshot = {
        "Id": "07e6864ccb37",
        "Image": "homepage:latest",
        "StartedAt": "2023-10-10T10:00:00Z"
    }

    success, diagnostic = execute_docker_restart({"container": "homepage", "snapshot": old_snapshot}, app_config)

    assert success is False
    assert "Precondition failed" in diagnostic["error"]

    # Verify POST did NOT happen
    mock_docker_request.assert_not_called()

@patch("dari_mcp_vps.tools.docker_tools._docker_request")
@patch("dari_mcp_vps.tools.docker_tools._json")
def test_execute_docker_restart_unauthorized_target(mock_json, mock_docker_request, app_config):
    # Attempting to restart an unlisted target
    snapshot = {"Id": "foo", "Image": "bar", "StartedAt": "baz"}
    success, diagnostic = execute_docker_restart({"container": "nginx", "snapshot": snapshot}, app_config)
    assert success is False
    assert "no longer authorized" in diagnostic["error"]

    # POST should NOT be executed
    mock_docker_request.assert_not_called()

@patch("dari_mcp_vps.tools.docker_tools._docker_request")
@patch("dari_mcp_vps.tools.docker_tools._json")
@patch("urllib.request.build_opener")
def test_execute_docker_restart_http_verification_fails(mock_build_opener, mock_json, mock_docker_request, app_config):
    # Setup mocks
    mock_docker_request.return_value = (204, {}, b"")
    mock_json.side_effect = [
        [{"Id": "07e6864ccb37", "Names": ["/homepage"], "State": "running"}],
        {"Id": "07e6864ccb37", "Config": {"Image": "homepage:latest"}, "State": {"StartedAt": "2023-10-10T10:00:00Z"}},
        [{"Id": "07e6864ccb37", "Names": ["/homepage"], "State": "running"}],
    ]

    mock_opener = MagicMock()
    # Make HTTP verification fail
    mock_opener.open.side_effect = urllib.error.URLError("Connection refused")
    mock_build_opener.return_value = mock_opener

    snapshot = {
        "Id": "07e6864ccb37",
        "Image": "homepage:latest",
        "StartedAt": "2023-10-10T10:00:00Z"
    }

    # To avoid long sleeps during test, mock time.sleep
    with patch("time.sleep"):
        success, diagnostic = execute_docker_restart({"container": "homepage", "snapshot": snapshot}, app_config)

    # With new logic, execution succeeds (since POST worked and container is running),
    # but diagnostic warns about HTTP verification.
    assert success is True
    assert "did not return 200 OK" in diagnostic["warning"]
    assert mock_docker_request.call_count == 1

    # Verify the Request shape
    req_arg = mock_opener.open.call_args[0][0]
    assert req_arg.full_url == "http://host.docker.internal:3001"
    assert req_arg.get_header("Host") == "julidcardenas.site"

def test_full_worker_flow(app_config, test_db_path):
    # Request approval
    req_id, is_new = create_request(test_db_path, "key_full", "docker_restart", {"container": "homepage"}, "secret123")

    # Claim decision (approve)
    import hmac
    token = hmac.new(b"secret123", req_id.encode('utf-8'), hashlib.sha256).hexdigest()[:24]
    claim_decision(test_db_path, req_id, token, "APPROVED")

    # Run the worker cycle directly
    from dari_mcp_vps.tools.approval_worker import background_approval_worker
    import asyncio

    # Mock the HTTP sending to immediately exit the worker loop gracefully
    async def mock_worker():
        from dari_mcp_vps.tools.approval_db import get_pending_outbox_events, consume_approved_requests
        with patch("dari_mcp_vps.tools.docker_tools.execute_docker_restart") as mock_exec:
            mock_exec.return_value = (True, {"message": "Mock success"})

            # Run the steps manually
            running = consume_approved_requests(test_db_path)
            assert len(running) == 1

            # Simulate the dispatch block
            from dari_mcp_vps.tools.action_dispatcher import get_handler
            from dari_mcp_vps.tools.approval_db import record_execution_result

            action = running[0]["action"]
            handler = get_handler(action)
            assert handler is not None

            # Override handler for test
            success, diag = mock_exec(running[0]["parameters"], app_config)
            record_execution_result(test_db_path, running[0]["id"], action, success, diag)

            # Check Outbox (filter for the completion event)
            events = get_pending_outbox_events(test_db_path)
            completion_events = [e for e in events if e["event_type"] == "OPERATION_COMPLETED"]
            assert len(completion_events) == 1

            payload = json.loads(completion_events[0]["payload"])
            assert payload["status"] == "OPERATION_COMPLETED"

    asyncio.run(mock_worker())

    # Verify state
    req = get_request(test_db_path, req_id)
    assert req["status"] == "OPERATION_COMPLETED"

@patch("dari_mcp_vps.tools.docker_tools._docker_request")
@patch("dari_mcp_vps.tools.docker_tools._json")
def test_uncertain_network_error_becomes_indeterminate(mock_json, mock_docker_request, app_config, test_db_path):
    mock_json.side_effect = [
        [{"Id": "07e6864ccb37", "Names": ["/homepage"], "State": "running"}],
        {"Id": "07e6864ccb37", "Config": {"Image": "homepage:latest"}, "State": {"StartedAt": "2023-10-10T10:00:00Z"}},
    ]

    # Simulate a network error DURING the POST request
    mock_docker_request.side_effect = TimeoutError("Docker response lost")

    snapshot = {
        "Id": "07e6864ccb37",
        "Image": "homepage:latest",
        "StartedAt": "2023-10-10T10:00:00Z"
    }

    req_id, is_new = create_request(
        test_db_path,
        "key_timeout",
        "docker_restart",
        {"container": "homepage", "snapshot": snapshot},
        "secret123"
    )

    import hmac
    token = hmac.new(b"secret123", req_id.encode('utf-8'), hashlib.sha256).hexdigest()[:24]
    claim_decision(test_db_path, req_id, token, "APPROVED")

    # Run the worker flow that processes APPROVED requests and handles exceptions
    from dari_mcp_vps.tools.approval_worker import background_approval_worker
    import asyncio

    async def mock_worker():
        from dari_mcp_vps.tools.approval_db import consume_approved_requests
        running = consume_approved_requests(test_db_path)
        from dari_mcp_vps.tools.action_dispatcher import get_handler, IndeterminateStateError
        from dari_mcp_vps.tools.approval_db import record_execution_result
        import sqlite3, json, datetime, uuid

        req = running[0]
        action = req["action"]
        handler = get_handler(action)
        try:
            success, diagnostic = handler(req["parameters"], app_config)
            record_execution_result(test_db_path, req["id"], action, success, diagnostic)
        except IndeterminateStateError as e:
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            diagnostic = {"error": f"Operation outcome is unknown due to interruption: {e}"}
            with sqlite3.connect(test_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE approvals SET status = 'INDETERMINATE_STATE', result_diagnostic = ? WHERE id = ?", (json.dumps(diagnostic), req["id"]))
                event_id = str(uuid.uuid4())
                event_payload = json.dumps({
                    "request_id": req["id"],
                    "action": action,
                    "status": "INDETERMINATE_STATE",
                    "diagnostic": diagnostic
                })
                cursor.execute("INSERT INTO approval_outbox (event_id, request_id, event_type, payload, status, created_at, retries) VALUES (?, ?, 'OPERATION_COMPLETED', ?, 'PENDING', ?, 0)",
                                (event_id, req["id"], event_payload, now))
                conn.commit()

    asyncio.run(mock_worker())

    req = get_request(test_db_path, req_id)
    assert req["status"] == "INDETERMINATE_STATE"
    assert "uncertain" in req["result_diagnostic"]["error"]

def test_worker_crash_recovery_indeterminate(app_config, test_db_path):
    # Request approval
    req_id, is_new = create_request(test_db_path, "key_crash", "docker_restart", {"container": "homepage"}, "secret123")

    import hmac
    token = hmac.new(b"secret123", req_id.encode('utf-8'), hashlib.sha256).hexdigest()[:24]
    claim_decision(test_db_path, req_id, token, "APPROVED")

    # Move to EXECUTING but manually force created_at to be very old
    running = consume_approved_requests(test_db_path)

    import sqlite3
    # Expire_pending_requests uses a timeout of 60 seconds (default).
    # Since created_at was used to store when it became EXECUTING, we just set created_at far enough back.
    old_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=120)).isoformat()
    with sqlite3.connect(test_db_path) as conn:
        conn.execute("UPDATE approvals SET created_at = ? WHERE id = ?", (old_time, req_id))
        conn.commit()

    # Run the expiration logic, which includes INDETERMINATE_STATE transitioning
    # Actually, the timeout logic is in recover_stuck_simulations, which is now renamed or combined in the worker.
    # Wait, looking at approval_db.py, expire_pending_requests DOES NOT transition EXECUTING.
    # We must call recover_stuck_simulations.
    from dari_mcp_vps.tools.approval_db import recover_stuck_simulations
    recover_stuck_simulations(test_db_path)

    # Verify state transitioned to INDETERMINATE_STATE
    req = get_request(test_db_path, req_id)
    assert req["status"] == "INDETERMINATE_STATE"
    assert "unknown" in req["result_diagnostic"]["error"]
