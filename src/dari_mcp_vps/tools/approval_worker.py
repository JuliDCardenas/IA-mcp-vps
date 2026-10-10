import asyncio
import logging
import urllib.request
import urllib.error
import json
import functools
from dari_mcp_vps.tools.approval_db import (
    init_db,
    expire_pending_requests,
    transition_to_running,
    record_simulation_result,
    get_pending_outbox_events,
    mark_outbox_event_sent
)

logger = logging.getLogger(__name__)

async def background_approval_worker(app_config):
    db_path = app_config.approval_db_path
    webhook_url = app_config.approval_n8n_webhook_url
    webhook_key = app_config.approval_n8n_webhook_key

    loop = asyncio.get_running_loop()

    while True:
        try:
            # 1. Expire pending requests
            expire_pending_requests(db_path)

            # 2. Pick up approved requests and run simulation
            running = transition_to_running(db_path)
            for req in running:
                # Simulate a brief delay (e.g. validating state, dry run)
                await asyncio.sleep(2)

                diagnostic = {
                    "simulation_log": f"Simulated action {req['action']}",
                    "dry_run_success": True,
                    "changes_preview": ["+ config_simulated.yaml"]
                }

                record_simulation_result(db_path, req["id"], success=True, diagnostic=diagnostic)

            # 3. Process outbox notifications
            if webhook_url:
                events = get_pending_outbox_events(db_path)
                for event in events:
                    req_data = json.dumps(json.loads(event["payload"])).encode("utf-8")
                    req = urllib.request.Request(
                        webhook_url,
                        data=req_data,
                        headers={
                            "Content-Type": "application/json",
                            "X-Approval-Webhook-Key": webhook_key,
                            "X-Event-Type": event["event_type"]
                        },
                        method="POST"
                    )

                    def send_webhook(req):
                        with urllib.request.urlopen(req, timeout=10) as r:
                            return r.status

                    try:
                        status_code = await loop.run_in_executor(None, functools.partial(send_webhook, req))
                        if 200 <= status_code < 300:
                            mark_outbox_event_sent(db_path, event["event_id"])
                    except Exception as e:
                        logger.warning(f"Failed to send approval webhook event {event['event_id']}: {e}")

        except Exception as e:
            logger.error(f"Error in background_approval_worker: {e}")

        await asyncio.sleep(5)
