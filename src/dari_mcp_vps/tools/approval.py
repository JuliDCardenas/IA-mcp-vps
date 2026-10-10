import json
import time
import datetime
import sqlite3
from typing import Any, Dict
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from dari_mcp_vps.tools.approval_db import (
    create_request,
    get_request,
    claim_decision
)

def register_approval_tools(mcp: FastMCP, app_config):
    db_path = app_config.approval_db_path

    @mcp.tool(tags=["agy"], annotations={"readOnlyHint": False})
    def approval_simulate_request(
        idempotency_key: str,
        action: str,
        parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Request a simulated action that requires durable approval."""
        if not app_config.approval_enabled:
            return {"error": "Approval feature is disabled by configuration"}

        if not app_config.approval_telegram_user_id or not app_config.approval_telegram_chat_id:
            return {"error": "Approval system is not configured"}

        try:
            req_id, is_new = create_request(
                db_path,
                idempotency_key,
                action,
                parameters,
                app_config.approval_webhook_secret
            )

            return {
                "request_id": req_id,
                "status": "PENDING" if is_new else "ALREADY_EXISTS_CHECK_STATUS",
                "message": "Approval request recorded. Waiting for decision."
            }
        except ValueError as e:
            return {"error": str(e)}

    @mcp.tool(tags=["agy"], annotations={"readOnlyHint": True})
    def approval_status(request_id: str) -> Dict[str, Any]:
        """Check the status of an approval request."""
        if not app_config.approval_enabled:
            return {"error": "Approval feature is disabled by configuration"}
        req = get_request(db_path, request_id)
        if not req:
            return {"error": "Request not found"}

        return {
            "request_id": req["id"],
            "status": req["status"],
            "action": req["action"],
            "result_diagnostic": req["result_diagnostic"]
        }

    @mcp.tool(tags=["agy"], annotations={"readOnlyHint": True})
    def approval_wait(request_id: str, timeout_seconds: int = 10) -> Dict[str, Any]:
        """Wait a bounded time for an approval request to reach a terminal state."""
        if not app_config.approval_enabled:
            return {"error": "Approval feature is disabled by configuration"}
        timeout_seconds = min(max(int(timeout_seconds), 1), 60)
        poll_interval = 2
        deadline = time.monotonic() + timeout_seconds

        while time.monotonic() < deadline:
            req = get_request(db_path, request_id)
            if not req:
                return {"error": "Request not found"}

            if req["status"] not in ("PENDING", "APPROVED", "RUNNING_SIMULATION", "EXECUTING"):
                return {
                    "request_id": req["id"],
                    "status": req["status"],
                    "action": req["action"],
                    "result_diagnostic": req["result_diagnostic"],
                    "wait_timed_out": False
                }

            time_left = max(0, deadline - time.monotonic())
            time.sleep(min(poll_interval, time_left))

        # Timeout reached, return current state
        req = get_request(db_path, request_id)
        if not req:
            return {"error": "Request not found (deleted during wait)"}

        # Ensure we don't return PENDING if it has logically expired but worker hasn't cleaned it yet
        if req["status"] == "PENDING":
            now_dt = datetime.datetime.now(datetime.timezone.utc)
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT expires_at FROM approvals WHERE id = ?", (request_id,))
                row = cursor.fetchone()
                if row:
                    expires_at_dt = datetime.datetime.fromisoformat(row[0])
                    if now_dt >= expires_at_dt:
                        req["status"] = "EXPIRED"

        return {
            "request_id": req["id"],
            "status": req["status"],
            "action": req["action"],
            "result_diagnostic": req["result_diagnostic"],
            "wait_timed_out": True
        }

    # Use add_route or router depending on FastMCP version. FastMCP provides a custom_route decorator
    try:
        @mcp.custom_route("/webhook/approval-decision", methods=["POST"])
        async def approval_decision_webhook(request: Request):
            if not app_config.approval_enabled:
                return JSONResponse({"error": "Feature disabled"}, status_code=503)
            if not app_config.approval_webhook_secret:
                return JSONResponse({"error": "Webhook secret not configured"}, status_code=500)

            # Verify auth securely to prevent timing attacks
            auth_header = request.headers.get("Authorization")
            expected_auth = f"Bearer {app_config.approval_webhook_secret}"

            import hmac
            if not auth_header or not hmac.compare_digest(auth_header.encode('utf-8'), expected_auth.encode('utf-8')):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)

            try:
                body = await request.json()
                if not isinstance(body, dict):
                    return JSONResponse({"error": "Invalid payload format, expected object"}, status_code=400)
            except Exception:
                return JSONResponse({"error": "Invalid JSON payload"}, status_code=400)

            user_id = body.get("user_id")
            chat_id = body.get("chat_id")
            request_id = body.get("request_id")
            capability_token = body.get("capability_token")
            decision = body.get("decision")

            # Validate types and bounds
            if not isinstance(user_id, (str, int)) or not isinstance(chat_id, (str, int)):
                return JSONResponse({"error": "Invalid ID types"}, status_code=400)
            if not isinstance(request_id, str) or not isinstance(capability_token, str) or len(capability_token) > 64:
                return JSONResponse({"error": "Invalid tokens"}, status_code=400)

            # Validate user identity strictly
            if str(user_id) != app_config.approval_telegram_user_id or str(chat_id) != app_config.approval_telegram_chat_id:
                return JSONResponse({"error": "Unauthorized user or chat"}, status_code=403)

            if not request_id or not capability_token or decision not in ("APPROVED", "REJECTED"):
                return JSONResponse({"error": "Invalid parameters"}, status_code=400)

            success = claim_decision(db_path, request_id, capability_token, decision)

            if success:
                return JSONResponse({"status": "Success", "message": f"Decision {decision} recorded."})
            else:
                return JSONResponse({"error": "Failed to claim decision. It may be expired, already claimed, or the token is invalid."}, status_code=400)
    except AttributeError:
        if app_config.approval_enabled:
            import logging
            logging.getLogger(__name__).error("FastMCP does not support custom_route. Cannot register approval callback.")
            raise RuntimeError("FastMCP version does not support required custom_route for approvals.")
