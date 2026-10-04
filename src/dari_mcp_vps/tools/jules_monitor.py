import asyncio
import json
import logging
import urllib.error
import urllib.request
from contextlib import asynccontextmanager

from dari_mcp_vps.tools.jules_db import (
    get_active_jobs,
    get_pending_events,
    mark_event_sent,
    record_event,
    update_job_status,
)

logger = logging.getLogger(__name__)

async def dispatch_webhooks(db_path, webhook_url, webhook_key):
    """Helper to dispatch pending webhooks asynchronously."""
    events = get_pending_events(db_path)
    if not events:
        return

    for event in events:
        headers = {"Content-Type": "application/json"}
        if webhook_key:
            headers["X-Jules-Webhook-Key"] = webhook_key

        req = urllib.request.Request(
            webhook_url,
            method="POST",
            headers=headers,
            data=event["payload"].encode("utf-8")
        )
        try:
            import functools
            loop = asyncio.get_running_loop()
            open_func = functools.partial(urllib.request.urlopen, req, timeout=10)
            with await loop.run_in_executor(None, open_func) as resp:
                if 200 <= resp.status < 300:
                    mark_event_sent(db_path, event["id"])
        except urllib.error.HTTPError as e:
            logger.warning(f"Webhook HTTP failure for event {event['id']}: {e.code}")
        except Exception as e:
            # Webhook failure must not lose event. Leave it PENDING.
            logger.warning(f"Failed to deliver webhook for event {event['id']}: {str(e)[:100]}")

async def background_monitor(app_config):
    """Background task to poll Jules API and dispatch webhooks to n8n."""
    db_path = app_config.jules_db_path
    webhook_url = app_config.n8n_webhook_url
    webhook_key = app_config.n8n_webhook_key

    while True:
        try:
            # 1. Dispatch Webhooks with bounded backoff (if a webhook fails, it will wait for the next loop)
            # Webhooks retry every 60s indefinitely by leaving them PENDING, acting as a bounded interval backoff.
            if webhook_url:
                await dispatch_webhooks(db_path, webhook_url, webhook_key)

            # 2. Poll Jules API for active sessions
            if app_config.jules_api_key:
                active_jobs = get_active_jobs(db_path)
                for job in active_jobs:
                    session_id = job["jules_agent_job_id"]
                    req_url = f"{app_config.jules_api_url}/{session_id}"

                    req = urllib.request.Request(
                        req_url,
                        method="GET",
                        headers={"X-Goog-Api-Key": app_config.jules_api_key}
                    )

                    try:
                        import functools
                        loop = asyncio.get_running_loop()
                        open_func = functools.partial(urllib.request.urlopen, req, timeout=10)
                        with await loop.run_in_executor(None, open_func) as resp:
                            session_data = json.loads(resp.read().decode("utf-8"))

                        remote_state = session_data.get("state")
                        if not remote_state:
                            continue

                        last_known_state = job.get("remote_state")

                        if remote_state != last_known_state:
                            # We have a state transition
                            pr_url = None
                            context_msg = None

                            if remote_state == "COMPLETED":
                                outputs = session_data.get("outputs", [])
                                for out in outputs:
                                    if "pullRequest" in out and "url" in out["pullRequest"]:
                                        pr_url = out["pullRequest"]["url"]
                                        context_msg = out["pullRequest"].get("title", "")

                            # Payload follows bounded contract
                            payload = {
                                "event_type": "STATE_CHANGED",
                                "task_id": job["id"],
                                "remote_session_id": session_id,
                                "repository": job["repo_name"],
                                "remote_state": remote_state,
                                "session_url": f"https://jules.google.com/session/{session_id.split('/')[-1]}",
                                "pr_url": pr_url,
                                "context": context_msg
                            }

                            # Strip Nones
                            payload = {k: v for k, v in payload.items() if v is not None}

                            # The event gets recorded and its event_id and timestamp are auto-generated in DB
                            record_event(db_path, job["id"], "STATE_CHANGED", payload)

                            # Update SQLite with new remote state.
                            # If completed or failed, we mark local status as well so we stop polling.
                            local_status = job["status"]
                            if remote_state in ("COMPLETED", "FAILED"):
                                local_status = remote_state
                            elif remote_state in ("AWAITING_PLAN_APPROVAL", "AWAITING_USER_FEEDBACK"):
                                local_status = "ESPERANDO_FEEDBACK"

                            update_job_status(db_path, job["id"], local_status, remote_state)

                    except urllib.error.HTTPError as e:
                        # 404 means the session was deleted remotely, we should probably stop polling
                        if e.code == 404:
                            update_job_status(db_path, job["id"], "FALLIDO", "NOT_FOUND")
                        logger.warning(f"Jules API HTTP error for job {job['id']}: {e.code}")
                    except Exception as e:
                        # Polling/network failure is NOT a failed coding session.
                        logger.warning(f"Polling failure for job {job['id']}: {e}")

        except Exception as e:
            logger.error(f"Error in Jules background monitor loop: {e}")

        await asyncio.sleep(60)

def monitor_lifespan_factory(app_config):
    @asynccontextmanager
    async def monitor_lifespan(server):
        # We only start the monitor if the DB can be initialized (it might already be via tools, but better safe)
        from dari_mcp_vps.tools.jules_db import init_db
        try:
            init_db(app_config.jules_db_path)
        except Exception as e:
            logger.error(f"Failed to initialize Jules database for monitor: {e}")
            yield
            return

        task = asyncio.create_task(background_monitor(app_config))
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    return monitor_lifespan
