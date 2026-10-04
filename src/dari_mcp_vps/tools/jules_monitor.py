import asyncio
import json
import logging
import urllib.error
import urllib.request
from contextlib import asynccontextmanager

import datetime
from dari_mcp_vps.tools.jules_db import (
    get_active_jobs,
    get_pending_events,
    mark_event_sent,
    record_event,
    update_job_status,
    is_activity_processed,
    record_activity_processed,
    record_activity_and_event,
    set_followup_pending,
    update_job_activities_cursor,
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

                        # Also check activities with pagination
                        all_activities = []
                        next_page_token = job.get("activities_cursor")
                        page_count = 0

                        while page_count < 5:  # Bounded budget per cycle
                            activities_url = f"{app_config.jules_api_url}/{session_id}/activities"
                            if next_page_token:
                                activities_url += f"?pageToken={next_page_token}"

                            req_activities = urllib.request.Request(
                                activities_url,
                                method="GET",
                                headers={"X-Goog-Api-Key": app_config.jules_api_key}
                            )

                            def fetch_and_parse(req):
                                with urllib.request.urlopen(req, timeout=10) as r:
                                    return json.loads(r.read().decode("utf-8"))

                            activities_data = await loop.run_in_executor(None, functools.partial(fetch_and_parse, req_activities))

                            all_activities.extend(activities_data.get("activities", []))
                            next_page_token = activities_data.get("nextPageToken")
                            if not next_page_token:
                                break
                            page_count += 1

                        update_job_activities_cursor(db_path, job["id"], next_page_token)

                        new_activities_found = False
                        seen_new_terminal_activity = False

                        # If we have a followup pending, check if it expired (1 hour)
                        followup_pending_since = job.get("followup_pending_since")
                        pending_since_dt = None
                        if followup_pending_since:
                            pending_since_dt = datetime.datetime.fromisoformat(followup_pending_since)
                            if datetime.datetime.now(datetime.timezone.utc) - pending_since_dt > datetime.timedelta(hours=1):
                                set_followup_pending(db_path, job["id"], False)
                                followup_pending_since = None
                                pending_since_dt = None

                        job_updated_at = datetime.datetime.fromisoformat(job["updated_at"])

                        for activity in all_activities:
                            activity_id = activity.get("id")
                            if not activity_id or is_activity_processed(db_path, activity_id):
                                continue

                            new_activities_found = True

                            activity_type = activity.get("activityType", "")

                            # Baseline check: to avoid replaying history (e.g. from a COMPLETED task
                            # before we sent feedback), we ignore events created before the followup was requested.
                            # If no followup is active, we just ensure it's not super old compared to job creation/update
                            create_time_str = activity.get("createTime")
                            is_new_event = True
                            if create_time_str:
                                # Jules returns "2024-10-04T12:00:00Z", replace Z with +00:00 for fromisoformat
                                act_dt = datetime.datetime.fromisoformat(create_time_str.replace("Z", "+00:00"))
                                if pending_since_dt and act_dt < pending_since_dt - datetime.timedelta(seconds=5):
                                    is_new_event = False
                                elif not pending_since_dt and act_dt < job_updated_at - datetime.timedelta(minutes=5):
                                    # Just migrating old jobs, ignore old activities for notifications
                                    is_new_event = False

                            if is_new_event and (activity_type in ("SESSION_COMPLETED", "SESSION_FAILED") or "sessionCompleted" in activity or "sessionFailed" in activity):
                                seen_new_terminal_activity = True

                            # Determine what kind of activity it is
                            event_type = None
                            context_msg = None

                            if is_new_event:
                                if "agentMessaged" in activity:
                                    event_type = "AGENT_MESSAGE"
                                    msg = activity["agentMessaged"].get("agentMessage", "")
                                    context_msg = msg[:500] + ("..." if len(msg) > 500 else "")
                                elif "planGenerated" in activity:
                                    event_type = "PLAN_GENERATED"
                                    context_msg = "A new plan has been generated."
                                elif "sessionFailed" in activity:
                                    event_type = "SESSION_FAILED"
                                    context_msg = activity["sessionFailed"].get("reason", "Unknown failure reason")
                                elif "sessionCompleted" in activity:
                                    event_type = "SESSION_COMPLETED"
                                    context_msg = "Session completed successfully."
                                elif activity_type in ("SESSION_COMPLETED", "SESSION_FAILED"):
                                    event_type = activity_type
                                    context_msg = f"Session reached terminal state: {activity_type}"

                            # If it's one of our interesting activities AND it's new, emit an event
                            if event_type:
                                pr_url = None
                                if remote_state == "COMPLETED":
                                    outputs = session_data.get("outputs", [])
                                    for out in outputs:
                                        if "pullRequest" in out and "url" in out["pullRequest"]:
                                            pr_url = out["pullRequest"]["url"]
                                            if not context_msg or "Session completed" in context_msg:
                                                context_msg = out["pullRequest"].get("title", "")

                                payload = {
                                    "event_type": event_type,
                                    "task_id": job["id"],
                                    "remote_session_id": session_id,
                                    "repository": job["repo_name"],
                                    "remote_state": remote_state,
                                    "session_url": f"https://jules.google.com/session/{session_id.split('/')[-1]}",
                                    "pr_url": pr_url,
                                    "context": context_msg,
                                    "activity_id": activity_id
                                }

                                # Omit remote_state from AGENT_MESSAGE to avoid confusion
                                if event_type == "AGENT_MESSAGE":
                                    del payload["remote_state"]

                                payload = {k: v for k, v in payload.items() if v is not None}
                                record_activity_and_event(db_path, job["id"], activity_id, event_type, payload)
                            else:
                                record_activity_processed(db_path, job["id"], activity_id)

                        # If state changed, OR if it's terminal and we just processed the completion activity
                        if remote_state != last_known_state and not seen_new_terminal_activity:
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

                            local_status = job["status"]

                            # Determine correct local status based on new remote state
                            if remote_state in ("COMPLETED", "FAILED"):
                                local_status = remote_state
                            elif remote_state in ("AWAITING_PLAN_APPROVAL", "AWAITING_USER_FEEDBACK"):
                                local_status = "ESPERANDO_FEEDBACK"
                            else:
                                local_status = "EN_PROGRESO"

                            # If remote state changed to a non-terminal state, clear followup
                            if followup_pending_since and remote_state not in ("COMPLETED", "FAILED", last_known_state):
                                set_followup_pending(db_path, job["id"], False)
                                followup_pending_since = None

                            update_job_status(db_path, job["id"], local_status, remote_state)

                        # If we saw a NEW terminal activity while tracking, clear followup
                        if followup_pending_since and seen_new_terminal_activity:
                            set_followup_pending(db_path, job["id"], False)
                            followup_pending_since = None

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
