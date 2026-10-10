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
    transition_to_running,
    record_simulation_result,
    get_pending_outbox_events
)
from dari_mcp_vps.tools.approval import register_approval_tools
from fastmcp import FastMCP

@pytest.fixture
def test_db_path(tmp_path):
    return str(tmp_path / "test_approvals.db")

@pytest.fixture
def app_config(test_db_path):
    os.environ["IA_MCP_VPS_CONFIG"] = "config.example.yaml"
    os.environ["APPROVAL_DB_PATH"] = test_db_path
    os.environ["APPROVAL_WEBHOOK_SECRET"] = "secret123"
    os.environ["APPROVAL_TELEGRAM_USER_ID"] = "1111"
    os.environ["APPROVAL_TELEGRAM_CHAT_ID"] = "2222"
    config = load_config()
    init_db(test_db_path)
    return config

def test_create_and_get_request(test_db_path, app_config):
    params = {"target": "foo"}
    req_id, token = create_request(test_db_path, "key1", "restart", params, "secret")

    assert req_id.startswith("a_")

    req = get_request(test_db_path, req_id)
    assert req is not None
    assert req["action"] == "restart"
    assert req["status"] == "PENDING"
    assert req["parameters"] == params

def test_idempotency(test_db_path, app_config):
    params = {"target": "foo"}
    req_id1, token1 = create_request(test_db_path, "key1", "restart", params, "secret")

    # Same key and params -> should return existing
    req_id2, token2 = create_request(test_db_path, "key1", "restart", params, "secret")

    assert req_id1 == req_id2
    assert token2 == "ALREADY_EXISTS"

    # Same key, different params -> should reject
    with pytest.raises(ValueError, match="conflict"):
        create_request(test_db_path, "key1", "restart", {"target": "bar"}, "secret")

def test_claim_decision(test_db_path, app_config):
    req_id, token = create_request(test_db_path, "key2", "stop", {}, "secret")

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
    req_id, token = create_request(test_db_path, "key3", "start", {}, "secret", ttl_seconds=-10)

    expire_pending_requests(test_db_path)

    req = get_request(test_db_path, req_id)
    assert req["status"] == "EXPIRED"

    # Should not be claimable
    assert not claim_decision(test_db_path, req_id, token, "APPROVED")

def test_transition_and_simulate(test_db_path, app_config):
    req_id, token = create_request(test_db_path, "key4", "run", {}, "secret")
    claim_decision(test_db_path, req_id, token, "APPROVED")

    running = transition_to_running(test_db_path)
    assert len(running) == 1
    assert running[0]["id"] == req_id

    req = get_request(test_db_path, req_id)
    assert req["status"] == "RUNNING_SIMULATION"

    record_simulation_result(test_db_path, req_id, True, {"log": "ok"})
    req = get_request(test_db_path, req_id)
    assert req["status"] == "SIMULATED_SUCCESS"
    assert req["result_diagnostic"]["log"] == "ok"

@pytest.mark.asyncio
async def test_webhook_route(app_config):
    from starlette.testclient import TestClient
    from starlette.applications import Starlette
    from starlette.routing import Route

    os.environ["APPROVAL_ENABLED"] = "true"
    app_config.raw["approvals"] = {"enabled": "true"}

    mcp = FastMCP("test")
    register_approval_tools(mcp, app_config)

    app = Starlette()

    for route in mcp._additional_http_routes:
        if isinstance(route, dict):
            app.routes.append(Route(route["path"], route["handler"], methods=route.get("methods")))
        else:
            app.routes.append(route)

    client = TestClient(app)

    req_id, token = create_request(app_config.approval_db_path, "key5", "test_route", {}, "secret123")

    # Missing auth
    response = client.post("/webhook/approval-decision", json={})
    assert response.status_code == 401

    # Wrong auth
    response = client.post("/webhook/approval-decision", json={}, headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401

    # Correct auth, wrong user
    response = client.post("/webhook/approval-decision",
        json={"user_id": "999", "chat_id": "2222", "request_id": req_id, "capability_token": token, "decision": "APPROVED"},
        headers={"Authorization": "Bearer secret123"}
    )
    assert response.status_code == 403

    # Correct auth, correct identity, invalid decision
    response = client.post("/webhook/approval-decision",
        json={"user_id": "1111", "chat_id": "2222", "request_id": req_id, "capability_token": token, "decision": "MAYBE"},
        headers={"Authorization": "Bearer secret123"}
    )
    assert response.status_code == 400

    # Correct payload
    response = client.post("/webhook/approval-decision",
        json={"user_id": "1111", "chat_id": "2222", "request_id": req_id, "capability_token": token, "decision": "APPROVED"},
        headers={"Authorization": "Bearer secret123"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "Success"

    req = get_request(app_config.approval_db_path, req_id)
    assert req["status"] == "APPROVED"
