import json
import urllib.error
import urllib.request
from typing import Any

import datetime
from dari_mcp_vps.tools.jules_db import init_db, create_job, update_job_remote_id, get_job, update_job_status, set_followup_pending


def register_jules_tools(mcp: Any, app_config: Any) -> None:
    _db_initialized = False

    def _get_db() -> str:
        nonlocal _db_initialized
        db_path = app_config.jules_db_path
        if not _db_initialized:
            try:
                init_db(db_path)
                _db_initialized = True
            except Exception as e:
                # Isolate initialization failures
                raise RuntimeError(f"Failed to initialize Jules database: {e}")
        return db_path

    @mcp.tool(tags=["jules"], annotations={"readOnlyHint": False})
    def jules_request_coding_task(repo_name: str, task_description: str) -> dict[str, Any]:
        """Create a Jules coding session for a given repository and task."""
        if not app_config.jules_api_key:
            return {"error": "JULES_API_KEY is not configured", "status": "ERROR"}

        # Verify task description isn't empty
        if not task_description or not task_description.strip():
             return {"error": "task_description cannot be empty", "status": "ERROR"}

        try:
            db_path = _get_db()
        except RuntimeError as e:
            return {"error": str(e), "status": "ERROR"}

        job_id = create_job(db_path, repo_name, task_description)

        try:
            # First, fetch sources to resolve the repo_name to a source name with pagination
            next_page_token = None
            matched_sources = []

            while True:
                sources_url = f"{app_config.jules_api_url}/sources"
                if next_page_token:
                    sources_url += f"?pageToken={next_page_token}"

                req_sources = urllib.request.Request(
                    sources_url,
                    method="GET",
                    headers={"X-Goog-Api-Key": app_config.jules_api_key}
                )

                with urllib.request.urlopen(req_sources, timeout=10) as resp:
                    sources_data = json.loads(resp.read().decode("utf-8"))
                    for source in sources_data.get("sources", []):
                        name = source.get("name", "")
                        id_ = source.get("id", "")
                        # Match either exact canonical "owner/repo" or short "repo"
                        if name.endswith(f"/{repo_name}") or id_.endswith(f"/{repo_name}") or name == repo_name or id_ == repo_name:
                            matched_sources.append(name)

                    next_page_token = sources_data.get("nextPageToken")
                    if not next_page_token:
                        break

            if not matched_sources:
                update_job_status(db_path, job_id, "FALLIDO")
                return {"error": f"Repository '{repo_name}' not found in Jules sources.", "task_id": job_id, "status": "FALLIDO"}
            elif len(matched_sources) > 1:
                update_job_status(db_path, job_id, "FALLIDO")
                return {"error": f"Repository '{repo_name}' matches multiple sources ({', '.join(matched_sources)}). Please use canonical owner/repo name.", "task_id": job_id, "status": "FALLIDO"}

            source_name = matched_sources[0]

            # Now, create the session
            sessions_url = f"{app_config.jules_api_url}/sessions"
            payload = {
                "prompt": task_description,
                "sourceContext": {
                    "source": source_name,
                    "githubRepoContext": {
                        "startingBranch": "main"
                    }
                },
                "automationMode": "AUTO_CREATE_PR"
            }

            req_sessions = urllib.request.Request(
                sessions_url,
                method="POST",
                headers={
                    "X-Goog-Api-Key": app_config.jules_api_key,
                    "Content-Type": "application/json"
                },
                data=json.dumps(payload).encode("utf-8")
            )

            with urllib.request.urlopen(req_sessions, timeout=30) as resp:
                session_data = json.loads(resp.read().decode("utf-8"))
                remote_session_id = session_data.get("name") or session_data.get("id")

                if remote_session_id:
                    update_job_remote_id(db_path, job_id, remote_session_id)
                    return {
                        "message": "Tarea delegada con éxito. No es necesario esperar.",
                        "task_id": job_id,
                        "jules_agent_job_id": remote_session_id,
                        "status": "EN_PROGRESO"
                    }
                else:
                    # Malformed success response: we don't know the ID, but it might have been created.
                    update_job_status(db_path, job_id, "DESCONOCIDO")
                    return {"error": "Invalid response from Jules API: missing session ID", "task_id": job_id, "status": "DESCONOCIDO"}

        except urllib.error.HTTPError as e:
            # Confirmed failure from API
            update_job_status(db_path, job_id, "FALLIDO")
            try:
                # Redact first, then truncate
                full_body = e.read().decode('utf-8')
                if app_config.jules_api_key in full_body:
                    full_body = full_body.replace(app_config.jules_api_key, "***REDACTED***")
                error_body = full_body[:200]
            except Exception:
                error_body = "Unknown body"

            error_msg = f"Jules API HTTP error {e.code}: {error_body}"
            return {"error": error_msg, "task_id": job_id, "status": "FALLIDO"}
        except Exception as e:
            # Timeout or other network error AFTER we potentially sent the request.
            # Outcome is uncertain. Do NOT automatically retry.
            update_job_status(db_path, job_id, "DESCONOCIDO")
            full_err = str(e)
            if app_config.jules_api_key in full_err:
                full_err = full_err.replace(app_config.jules_api_key, "***REDACTED***")
            return {"error": f"Failed to contact Jules API or connection timed out: {full_err[:100]}", "task_id": job_id, "status": "DESCONOCIDO"}

    @mcp.tool(tags=["jules"], annotations={"readOnlyHint": False})
    def jules_reply_to_task(task_id: str, message: str) -> dict[str, Any]:
        """Send a follow-up message to an existing Jules session."""
        if not app_config.jules_api_key:
            return {"error": "JULES_API_KEY is not configured", "status": "ERROR"}

        if not message or not message.strip():
            return {"error": "message cannot be empty", "status": "ERROR"}

        try:
            db_path = _get_db()
        except RuntimeError as e:
            return {"error": str(e), "status": "ERROR"}

        job = get_job(db_path, task_id)
        if not job:
            return {"error": f"Task ID {task_id} not found", "status": "NOT_FOUND"}

        session_id = job.get("jules_agent_job_id")
        if not session_id:
            return {"error": f"Task ID {task_id} does not have a remote session ID", "status": "ERROR"}

        # We do not restrict this by local status (e.g. COMPLETED)
        # Let the API determine if it accepts the message.
        reply_url = f"{app_config.jules_api_url}/{session_id}:sendMessage"
        payload = {"prompt": message}

        req = urllib.request.Request(
            reply_url,
            method="POST",
            headers={
                "X-Goog-Api-Key": app_config.jules_api_key,
                "Content-Type": "application/json"
            },
            data=json.dumps(payload).encode("utf-8")
        )

        # Capture baseline before making the request to avoid missing fast responses during a slow POST
        request_start_time = datetime.datetime.now(datetime.timezone.utc).isoformat()

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                # Discard response body but ensure it was successful
                resp.read()

            try:
                set_followup_pending(db_path, task_id, True, pending_since_iso=request_start_time)
            except Exception as e:
                # Persistence failed but remote accepted. We must return success but inform of persistence issue.
                return {
                    "error": f"Message sent successfully to Jules, but failed to save local state tracking: {str(e)[:100]}",
                    "task_id": task_id,
                    "jules_agent_job_id": session_id,
                    "status": "SENT",
                    "message": "Message successfully sent to the remote session."
                }

            return {
                "task_id": task_id,
                "jules_agent_job_id": session_id,
                "status": "SENT",
                "message": "Message successfully sent to the remote session."
            }
        except urllib.error.HTTPError as e:
            try:
                # Redact first, then truncate
                full_body = e.read().decode('utf-8')
                if app_config.jules_api_key in full_body:
                    full_body = full_body.replace(app_config.jules_api_key, "***REDACTED***")
                error_body = full_body[:200]
            except Exception:
                error_body = "Unknown body"

            error_msg = f"Jules API HTTP error {e.code}: {error_body}"
            return {"error": error_msg, "task_id": task_id, "status": "ERROR"}
        except Exception as e:
            full_err = str(e)
            if app_config.jules_api_key in full_err:
                full_err = full_err.replace(app_config.jules_api_key, "***REDACTED***")

            # Outcome is uncertain (e.g. timeout), so we activate tracking to reconcile
            try:
                set_followup_pending(db_path, task_id, True, pending_since_iso=request_start_time)
            except Exception:
                pass

            return {"error": f"Failed to contact Jules API or connection timed out: {full_err[:100]}", "task_id": task_id, "status": "DESCONOCIDO"}

    @mcp.tool(tags=["jules"], annotations={"readOnlyHint": False})
    def jules_check_task_status(task_id: str) -> dict[str, Any]:
        """Check the status of a previously requested Jules coding task."""
        try:
            db_path = _get_db()
        except RuntimeError as e:
            return {"error": str(e), "status": "ERROR"}

        job = get_job(db_path, task_id)
        if not job:
            return {"error": f"Task ID {task_id} not found", "status": "NOT_FOUND"}

        return {
            "is_local_cache": True,
            "task_id": job["id"],
            "repo_name": job["repo_name"],
            "task_description": job["task_description"],
            "jules_agent_job_id": job["jules_agent_job_id"],
            "status": job["status"],
            "remote_state": job.get("remote_state"),
            "created_at": job["created_at"],
            "updated_at": job["updated_at"]
        }

    @mcp.tool(tags=["jules"], annotations={"readOnlyHint": False})
    def jules_get_task_activities(task_id: str, page_size: int = 20, page_token: str | None = None) -> dict[str, Any]:
        """Get a paginated list of activities for a specific Jules task.
        Use page_token from the previous response to get the next page.
        Do not query this repeatedly without a page token. Limits to 1-100 items per page."""
        if not app_config.jules_api_key:
            return {"error": "JULES_API_KEY is not configured"}

        page_size = max(1, min(100, page_size))

        try:
            db_path = _get_db()
        except RuntimeError as e:
            return {"error": str(e)}

        job = get_job(db_path, task_id)
        if not job:
            return {"error": f"Task ID {task_id} not found locally."}

        session_id = job.get("jules_agent_job_id")
        if not session_id:
            return {"error": f"Task ID {task_id} does not have a remote session ID."}

        url = f"{app_config.jules_api_url}/{session_id}/activities?pageSize={page_size}"
        if page_token:
            import urllib.parse
            url += f"&pageToken={urllib.parse.quote(page_token)}"

        req = urllib.request.Request(
            url,
            method="GET",
            headers={"X-Goog-Api-Key": app_config.jules_api_key}
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))

                # Format activities to reduce size/noise
                formatted_activities = []
                for act in data.get("activities", []):
                    # We just copy it but truncate any large fields
                    fmt_act = {"id": act.get("id"), "activityType": act.get("activityType"), "createTime": act.get("createTime")}

                    if "agentMessaged" in act:
                        msg = act["agentMessaged"].get("agentMessage", "")
                        fmt_act["agentMessage"] = msg[:1000] + ("..." if len(msg) > 1000 else "")
                    elif "planGenerated" in act:
                        plan = act["planGenerated"].get("plan", "A plan was generated.")
                        fmt_act["planGenerated"] = plan[:1000] + ("..." if len(plan) > 1000 else "")
                    elif "sessionCompleted" in act:
                        fmt_act["sessionCompleted"] = "Session completed successfully."
                    elif "sessionFailed" in act:
                        reason = act["sessionFailed"].get("reason", "Unknown error")
                        fmt_act["sessionFailed"] = reason[:500] + ("..." if len(reason) > 500 else "")

                    formatted_activities.append(fmt_act)

                return {
                    "source": "api",
                    "consulted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "activities": formatted_activities,
                    "nextPageToken": data.get("nextPageToken")
                }

        except urllib.error.HTTPError as e:
            try:
                full_body = e.read().decode('utf-8')
                if app_config.jules_api_key in full_body:
                    full_body = full_body.replace(app_config.jules_api_key, "***REDACTED***")
                error_body = full_body[:200]
            except Exception:
                error_body = "Unknown body"
            return {"error": f"Jules API HTTP error {e.code}: {error_body}"}
        except Exception as e:
            full_err = str(e)
            if app_config.jules_api_key in full_err:
                full_err = full_err.replace(app_config.jules_api_key, "***REDACTED***")
            return {"error": f"Failed to contact Jules API: {full_err[:100]}"}
