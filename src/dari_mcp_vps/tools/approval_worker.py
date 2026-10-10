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
    transition_to_running,
    record_simulation_result,
    get_pending_outbox_events,
    mark_outbox_event_sent,
    increment_outbox_retry,
    reset_stuck_simulations
)

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

    # 0. Recover any stuck tasks from a previous crash
    try:
        reset_stuck_simulations(db_path)
    except Exception as e:
        logger.error(f"Failed to reset stuck simulations: {e}")

    # Setup safe HTTP opener
    opener = urllib.request.build_opener(NoRedirectHandler())

    while True:
        try:
            # 1. Expire pending requests
            expire_pending_requests(db_path)

            # 2. Pick up approved requests and run simulation
            running = transition_to_running(db_path)
            for req in running:
                # Simulate deterministically, log output
                diagnostic = {
                    "simulation_log": f"Simulated action {req['action']} for idempotency key",
                    "dry_run_success": True,
                    "changes_preview": ["+ simulated_change.txt"]
                }
                record_simulation_result(db_path, req["id"], success=True, diagnostic=diagnostic)

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
