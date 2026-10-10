import pytest
import os
import json
import time
import datetime
import hashlib
from unittest.mock import patch, MagicMock
from dari_mcp_vps.config import load_config
from dari_mcp_vps.tools.approval_db import (
    init_db,
    create_request,
    get_request,
    claim_decision,
    expire_pending_requests,
    consume_approved_requests,
    record_execution_result,
    get_pending_outbox_events
)
from dari_mcp_vps.tools.approval import register_approval_tools
from fastmcp import FastMCP

@pytest.fixture
def test_db_path(tmp_path):
    return str(tmp_path / "test_approvals.db")

@pytest.fixture
def app_config(test_db_path, monkeypatch):
    monkeypatch.setenv("IA_MCP_VPS_CONFIG", "config.example.yaml")
    monkeypatch.setenv("APPROVAL_DB_PATH", test_db_path)
    monkeypatch.setenv("APPROVAL_WEBHOOK_SECRET", "secret123")
    monkeypatch.setenv("APPROVAL_N8N_WEBHOOK_KEY", "webhook123")
    monkeypatch.setenv("APPROVAL_N8N_WEBHOOK_URL", "https://example.com/webhook")
    monkeypatch.setenv("APPROVAL_TELEGRAM_USER_ID", "1111")
    monkeypatch.setenv("APPROVAL_TELEGRAM_CHAT_ID", "2222")
    config = load_config()
    init_db(test_db_path)
    return config

def test_create_and_get_request(test_db_path, app_config):
    params = {"target": "foo"}
    req_id, is_new = create_request(test_db_path, "key1", "approval_demo", params, "secret123")

    assert req_id.startswith("a_")

    req = get_request(test_db_path, req_id)
    assert req is not None
    assert req["action"] == "approval_demo"
    assert req["status"] == "PENDING"
    assert req["parameters"] == params

def test_idempotency(test_db_path, app_config):
    params = {"target": "foo"}
    req_id1, is_new1 = create_request(test_db_path, "key1", "approval_demo", params, "secret123")

    # Same key and params -> should return existing
    req_id2, is_new2 = create_request(test_db_path, "key1", "approval_demo", params, "secret123")

    assert req_id1 == req_id2

    # Same key, different params -> should reject
    with pytest.raises(ValueError, match="conflict"):
        create_request(test_db_path, "key1", "approval_demo", {"target": "bar"}, "secret123")

def test_claim_decision(test_db_path, app_config):
    req_id, is_new = create_request(test_db_path, "key2", "approval_demo", {}, "secret123")

    # We must compute the capability correctly to claim it
    import hmac
    token = hmac.new(b"secret123", req_id.encode('utf-8'), hashlib.sha256).hexdigest()[:24]

    # Try invalid token
    assert not claim_decision(test_db_path, req_id, "badtoken", "APPROVED")

    # Try valid token
    assert claim_decision(test_db_path, req_id, token, "APPROVED")

    # Verify status changed
    req = get_request(test_db_path, req_id)
    assert req["status"] == "APPROVED"

    # Try claiming again (should fail)
    assert not claim_decision(test_db_path, req_id, token, "REJECTED")

def test_expire_pending(test_db_path, app_config):
    now = datetime.datetime.now(datetime.timezone.utc)

    # Create in the past by passing a negative TTL
    req_id, is_new = create_request(test_db_path, "key3", "approval_demo", {}, "secret123", ttl_seconds=-10)

    import hmac
    token = hmac.new(b"secret123", req_id.encode('utf-8'), hashlib.sha256).hexdigest()[:24]

    expire_pending_requests(test_db_path)

    req = get_request(test_db_path, req_id)
    assert req["status"] == "EXPIRED"

    # Should not be claimable
    assert not claim_decision(test_db_path, req_id, token, "APPROVED")

def test_transition_and_simulate(test_db_path, app_config):
    from dari_mcp_vps.tools.approval_db import consume_approved_requests, record_execution_result
    req_id, is_new = create_request(test_db_path, "key4", "approval_demo", {}, "secret123")

    import hmac
    token = hmac.new(b"secret123", req_id.encode('utf-8'), hashlib.sha256).hexdigest()[:24]

    claim_decision(test_db_path, req_id, token, "APPROVED")

    running = consume_approved_requests(test_db_path)
    assert len(running) == 1
    assert running[0]["id"] == req_id

    record_execution_result(test_db_path, req_id, "approval_demo", True, {"log": "ok"})
    req = get_request(test_db_path, req_id)
    assert req["status"] == "SIMULATED_SUCCESS"
    assert req["result_diagnostic"]["log"] == "ok"

def test_config_validation(test_db_path, monkeypatch):
    monkeypatch.setenv("IA_MCP_VPS_CONFIG", "config.example.yaml")
    monkeypatch.setenv("APPROVAL_DB_PATH", test_db_path)

    # Missing enabled
    config = load_config()
    assert not config.approval_enabled

    # Missing secrets
    monkeypatch.setenv("APPROVAL_ENABLED", "true")
    config = load_config()
    assert not config.approval_enabled

    # Has all but invalid Telegram IDs
    monkeypatch.setenv("APPROVAL_WEBHOOK_SECRET", "secret123")
    monkeypatch.setenv("APPROVAL_N8N_WEBHOOK_KEY", "webhook123")
    monkeypatch.setenv("APPROVAL_TELEGRAM_USER_ID", "abc")
    monkeypatch.setenv("APPROVAL_TELEGRAM_CHAT_ID", "2222")
    monkeypatch.setenv("APPROVAL_N8N_WEBHOOK_URL", "https://example.com/webhook")
    config = load_config()
    assert not config.approval_enabled

    # Has invalid URL
    monkeypatch.setenv("APPROVAL_TELEGRAM_USER_ID", "1111")
    monkeypatch.setenv("APPROVAL_N8N_WEBHOOK_URL", "http://example.com/webhook")
    config = load_config()
    assert not config.approval_enabled

    # Valid
    monkeypatch.setenv("APPROVAL_N8N_WEBHOOK_URL", "https://example.com/webhook")
    config = load_config()
    assert config.approval_enabled

@pytest.mark.asyncio
async def test_webhook_route(app_config, monkeypatch):
    from starlette.testclient import TestClient
    from starlette.applications import Starlette
    from starlette.routing import Route

    monkeypatch.setenv("APPROVAL_ENABLED", "true")
    monkeypatch.setenv("APPROVAL_WEBHOOK_SECRET", "secret123")
    monkeypatch.setenv("APPROVAL_TELEGRAM_USER_ID", "1111")
    monkeypatch.setenv("APPROVAL_TELEGRAM_CHAT_ID", "2222")
    monkeypatch.setenv("APPROVAL_N8N_WEBHOOK_URL", "https://example.com")

    app_config.raw["approvals"] = {"enabled": "true", "webhook_secret": "secret123"}

    mcp = FastMCP("test")
    register_approval_tools(mcp, app_config)

    app = Starlette()

    for route in mcp._additional_http_routes:
        if isinstance(route, dict):
            app.routes.append(Route(route["path"], route["handler"], methods=route.get("methods")))
        else:
            app.routes.append(route)

    client = TestClient(app)

    # Create via actual Tool Call
    # Instead of digging into internals, just use the FastMCP method:
    import inspect
    tool = mcp.get_tool("approval_simulate_request")
    if inspect.iscoroutine(tool):
        tool = await tool

    res = tool.fn(idempotency_key="key5", action="approval_demo", parameters={})
    if inspect.iscoroutine(res):
        res = await res

    req_id = res["request_id"]

    # Get the capability token (we need to trigger the worker loop manually since it's an isolated test)
    from dari_mcp_vps.tools.approval_worker import background_approval_worker
    import asyncio
    import urllib.request

    # Mock outbox sending to capture the request payload containing the token
    captured_token = None
    class MockOpener:
        def open(self, req, timeout):
            nonlocal captured_token
            payload = json.loads(req.data.decode('utf-8'))
            captured_token = payload.get("capability_token")
            class MockResponse:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *args): pass
            return MockResponse()

    with patch("urllib.request.build_opener", return_value=MockOpener()):
        worker_task = asyncio.create_task(background_approval_worker(app_config))
        await asyncio.sleep(0.1) # Yield to worker
        worker_task.cancel()

    assert captured_token is not None

    # List instead of dict payload
    response = client.post("/webhook/approval-decision", json=[], headers={"Authorization": "Bearer secret123"})
    assert response.status_code == 400

    # Missing auth
    response = client.post("/webhook/approval-decision", json={})
    assert response.status_code == 401

    # Wrong auth
    response = client.post("/webhook/approval-decision", json={}, headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401

    # Correct auth, wrong user
    response = client.post("/webhook/approval-decision",
        json={"user_id": "999", "chat_id": "2222", "request_id": req_id, "capability_token": captured_token, "decision": "APPROVED"},
        headers={"Authorization": "Bearer secret123"}
    )
    assert response.status_code == 403

    # Correct auth, correct identity, invalid decision
    response = client.post("/webhook/approval-decision",
        json={"user_id": "1111", "chat_id": "2222", "request_id": req_id, "capability_token": captured_token, "decision": "MAYBE"},
        headers={"Authorization": "Bearer secret123"}
    )
    assert response.status_code == 400

    # Invalid payload token size
    response = client.post("/webhook/approval-decision",
        json={"user_id": "1111", "chat_id": "2222", "request_id": req_id, "capability_token": "A"*70, "decision": "APPROVED"},
        headers={"Authorization": "Bearer secret123"}
    )
    assert response.status_code == 400

    # Correct payload
    response = client.post("/webhook/approval-decision",
        json={"user_id": "1111", "chat_id": "2222", "request_id": req_id, "capability_token": captured_token, "decision": "APPROVED"},
        headers={"Authorization": "Bearer secret123"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "Success"

    req = get_request(app_config.approval_db_path, req_id)
    assert req["status"] == "APPROVED"

    assert len(captured_token) <= 24
