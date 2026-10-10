import asyncio
import logging
import urllib.request
import urllib.error
import json
import functools
import hmac
import hashlib
from dari_mcp_vps.tools.approval_db import (
    init_db,
    expire_pending_requests,
    recover_stuck_simulations,
    consume_approved_requests,
    record_execution_result,
    get_pending_outbox_events,
    mark_outbox_event_sent,
    increment_outbox_retry
)
from dari_mcp_vps.tools.action_dispatcher import get_handler

logger = logging.getLogger(__name__)

class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Disable redirects

async def background_approval_worker(app_config):
    db_path = app_config.approval_db_path
    webhook_url = app_config.approval_n8n_webhook_url
    webhook_key = app_config.approval_n8n_webhook_key
    app_secret = app_config.approval_webhook_secret

    # Check if disabled
    if not app_config.approval_enabled:
        return

    loop = asyncio.get_running_loop()

    # Setup safe HTTP opener
    opener = urllib.request.build_opener(NoRedirectHandler())

    while True:
        try:
            # 1. Expire pending requests and handle stuck executions
            expire_pending_requests(db_path)
            recover_stuck_simulations(db_path)

            # 2. Pick up approved requests, dispatch to internal handlers, and persist result transactionally
            running = consume_approved_requests(db_path)
            for req in running:
                action = req["action"]
                handler = get_handler(action)

                if handler:
                    from dari_mcp_vps.tools.action_dispatcher import IndeterminateStateError
                    try:
                        success, diagnostic = handler(req["parameters"], app_config)
                        record_execution_result(db_path, req["id"], action, success=success, diagnostic=diagnostic)
                    except IndeterminateStateError as e:
                        import sqlite3
                        import datetime
                        import uuid
                        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                        diagnostic = {"error": f"Operation outcome is unknown due to interruption: {e}"}
                        with sqlite3.connect(db_path) as conn:
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
                    except Exception as e:
                        success = False
                        diagnostic = {"error": f"Handler threw an exception before operation: {e}"}
                        record_execution_result(db_path, req["id"], action, success=success, diagnostic=diagnostic)
                else:
                    if action == "approval_demo":
                        success = True
                        diagnostic = {
                            "simulation_log": f"Simulated action {action}",
                            "dry_run_success": True,
                            "changes_preview": ["+ simulated_change.txt"]
                        }
                    else:
                        success = False
                        diagnostic = {"error": f"No internal handler registered for action '{action}'"}
                    record_execution_result(db_path, req["id"], action, success=success, diagnostic=diagnostic)

            # 3. Process outbox notifications
            if webhook_url and webhook_url.startswith("https://"):
                events = get_pending_outbox_events(db_path)
                for event in events:
                    # Reconstruct the capability token in memory for transmission
                    payload_dict = json.loads(event["payload"])
                    if event["event_type"] == "APPROVAL_REQUESTED":
                        req_id = payload_dict["request_id"]
                        capability_token = hmac.new(app_secret.encode('utf-8'), req_id.encode('utf-8'), hashlib.sha256).hexdigest()[:24]
                        payload_dict["capability_token"] = capability_token

                    req_data = json.dumps(payload_dict).encode("utf-8")
                    req_http = urllib.request.Request(
                        webhook_url,
                        data=req_data,
                        headers={
                            "Content-Type": "application/json",
                            "X-Approval-Webhook-Key": webhook_key,
                            "X-Event-Type": event["event_type"],
                            "X-Event-Id": event["event_id"]
                        },
                        method="POST"
                    )

                    def send_webhook(req_http):
                        with opener.open(req_http, timeout=5) as r:
                            return r.status

                    try:
                        status_code = await loop.run_in_executor(None, functools.partial(send_webhook, req_http))
                        if 200 <= status_code < 300:
                            mark_outbox_event_sent(db_path, event["event_id"])
                        else:
                            increment_outbox_retry(db_path, event["event_id"])
                    except Exception as e:
                        logger.warning(f"Failed to send approval webhook event {event['event_id']}: {e}")
                        increment_outbox_retry(db_path, event["event_id"])
            elif webhook_url:
                logger.error("Approval webhook URL is configured but not HTTPS, refusing to send.")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in background_approval_worker: {e}")

        await asyncio.sleep(2)
