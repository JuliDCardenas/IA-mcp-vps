import json
import urllib.error
import urllib.request
import urllib.parse
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

            try:
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
            except urllib.error.HTTPError as e:
                update_job_status(db_path, job_id, "FALLIDO")
                return {"error": f"Failed to resolve repository (HTTP {e.code})", "task_id": job_id, "status": "FALLIDO"}
            except Exception:
                update_job_status(db_path, job_id, "FALLIDO")
                return {"error": "Failed to resolve repository (network error or timeout)", "task_id": job_id, "status": "FALLIDO"}

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
                remote_state = session_data.get("state")

                if remote_session_id:
                    try:
                        update_job_remote_id(db_path, job_id, remote_session_id, remote_state)
                    except Exception:
                        return {
                            "error": "Failed to persist task locally, but remote session was created.",
                            "task_id": job_id,
                            "jules_agent_job_id": remote_session_id,
                            "status": "EN_PROGRESO"
                        }

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
            if e.code < 500:
                # Confirmed rejection from API
                update_job_status(db_path, job_id, "FALLIDO")
                return {"error": f"API rejected session creation (HTTP {e.code})", "task_id": job_id, "status": "FALLIDO"}
            else:
                # 5xx error, outcome is uncertain
                update_job_status(db_path, job_id, "DESCONOCIDO")
                return {"error": f"Failed to contact API or connection timed out during submission (HTTP {e.code})", "task_id": job_id, "status": "DESCONOCIDO"}
        except Exception:
            # Timeout or other network error AFTER we potentially sent the request.
            # Outcome is uncertain. Do NOT automatically retry.
            update_job_status(db_path, job_id, "DESCONOCIDO")
            return {"error": "Failed to contact API or connection timed out during submission", "task_id": job_id, "status": "DESCONOCIDO"}

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
                    "error": "Message sent successfully to Jules, but failed to save local state tracking",
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

            # Treat 5xx Server Errors as uncertain outcomes just like timeouts
            if 500 <= e.code < 600:
                try:
                    set_followup_pending(db_path, task_id, True, pending_since_iso=request_start_time)
                except Exception:
                    pass
                return {"error": error_msg, "task_id": task_id, "status": "DESCONOCIDO"}

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
            "remote_observed_at": job.get("remote_observed_at"),
            "remote_observation_error": job.get("remote_observation_error"),
            "created_at": job["created_at"],
            "updated_at": job["updated_at"]
        }

    @mcp.tool(tags=["jules"], annotations={"readOnlyHint": False})
    def jules_get_task_activities(
        task_id: str,
        page_size: int = 20,
        page_token: str | None = None,
        activity_id: str | None = None,
        content_offset: int = 0
    ) -> dict[str, Any]:
        """Get a paginated list of activities for a specific Jules task, or fetch a specific activity's full content in chunks.
        If `activity_id` is provided, fetches the requested activity in chunks of 8000 chars starting at `content_offset`.
        List Mode Limits: 1-100 items per page."""
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

        def fetch_page(token: str | None) -> dict[str, Any]:
            url = f"{app_config.jules_api_url}/{session_id}/activities?pageSize={page_size}"
            if token:
                url += f"&pageToken={urllib.parse.quote(token)}"

            req = urllib.request.Request(url, method="GET", headers={"X-Goog-Api-Key": app_config.jules_api_key})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))

        def redact_secrets(text: str) -> str:
            if not isinstance(text, str):
                return text
            if app_config.jules_api_key and app_config.jules_api_key in text:
                return text.replace(app_config.jules_api_key, "***REDACTED***")
            return text

        try:
            if activity_id:
                # Detail Mode: Search for the activity (bounded to 5 pages)
                current_token = page_token
                pages_checked = 0
                while pages_checked < 5:
                    data = fetch_page(current_token)
                    activities = data.get("activities", [])
                    for act in activities:
                        if act.get("id") == activity_id:
                            # Extract full content based on type
                            full_content = ""
                            if "agentMessaged" in act:
                                full_content = act["agentMessaged"].get("agentMessage", "")
                            elif "planGenerated" in act:
                                full_content = json.dumps(act["planGenerated"].get("plan", {}), indent=2)
                            elif "sessionFailed" in act:
                                full_content = act["sessionFailed"].get("reason", "Unknown error")
                            elif "sessionCompleted" in act:
                                full_content = "Session completed successfully."
                            else:
                                full_content = f"Unsupported activity type: {act.get('activityType', 'UNKNOWN')}"

                            full_content = redact_secrets(full_content)

                            total_length = len(full_content)
                            chunk_size = 8000

                            if content_offset < 0:
                                return {"error": "content_offset cannot be negative"}
                            if content_offset >= total_length:
                                fragment = ""
                                has_more = False
                            else:
                                fragment = full_content[content_offset:content_offset + chunk_size]
                                has_more = (content_offset + chunk_size) < total_length
                            has_more = (content_offset + chunk_size) < total_length

                            return {
                                "source": "api",
                                "consulted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                "activity_id": activity_id,
                                "fragment": fragment,
                                "total_length": total_length,
                                "has_more": has_more,
                                "next_content_offset": content_offset + chunk_size if has_more else None
                            }

                    current_token = data.get("nextPageToken")
                    if not current_token:
                        break
                    pages_checked += 1

                # If not found within the budget
                return {
                    "source": "api",
                    "consulted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "error": f"Activity {activity_id} not found within search budget.",
                    "nextPageToken": current_token
                }

            else:
                # List Mode
                data = fetch_page(page_token)
                formatted_activities = []
                for act in data.get("activities", []):
                    act_type = act.get("activityType")
                    if not act_type:
                        if "agentMessaged" in act: act_type = "AGENT_MESSAGED"
                        elif "planGenerated" in act: act_type = "PLAN_GENERATED"
                        elif "sessionCompleted" in act: act_type = "SESSION_COMPLETED"
                        elif "sessionFailed" in act: act_type = "SESSION_FAILED"
                        else: act_type = "UNKNOWN"

                    fmt_act = {"id": act.get("id"), "activityType": act_type, "createTime": act.get("createTime")}

                    def process_field(field_key, out_key, extract_fn):
                        if field_key in act:
                            raw_val = extract_fn(act)
                            val = redact_secrets(raw_val)
                            if len(val) > 1000:
                                fmt_act[out_key] = val[:1000] + "..."
                                fmt_act["is_truncated"] = True
                            else:
                                fmt_act[out_key] = val

                    process_field("agentMessaged", "agentMessage", lambda a: a["agentMessaged"].get("agentMessage", ""))

                    def extract_plan(a):
                        p = a["planGenerated"].get("plan", {})
                        if isinstance(p, dict):
                            steps = p.get("steps", [])
                            return f"Plan ID: {p.get('id', 'Unknown')}, Steps: {len(steps)}"
                        return str(p)
                    process_field("planGenerated", "planGenerated", extract_plan)

                    if "sessionCompleted" in act:
                        fmt_act["sessionCompleted"] = "Session completed successfully."

                    process_field("sessionFailed", "sessionFailed", lambda a: a["sessionFailed"].get("reason", "Unknown error"))

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
                full_body = redact_secrets(full_body)
                error_body = full_body[:200]
            except Exception:
                error_body = "Unknown body"
            return {"error": f"Jules API HTTP error {e.code}: {error_body}"}
        except Exception as e:
            full_err = str(e)
            full_err = redact_secrets(full_err)
            return {"error": f"Failed to contact Jules API: {full_err[:100]}"}
