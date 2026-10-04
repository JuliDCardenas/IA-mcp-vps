import json
import urllib.error
import urllib.request
from typing import Any

from dari_mcp_vps.tools.jules_db import init_db, create_job, update_job_remote_id, get_job, update_job_status


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

    @mcp.tool()
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

        # Phase 1: Fetch sources to resolve the repo_name to a source name with pagination
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
            # We don't read error_body for public errors
            return {"error": f"Jules API rejected GET /sources request with HTTP error {e.code}", "task_id": job_id, "status": "FALLIDO"}
        except Exception as e:
            # Before POST, any failure means it was not submitted
            update_job_status(db_path, job_id, "FALLIDO")
            return {"error": "Failed to fetch sources from Jules API before submission", "task_id": job_id, "status": "FALLIDO"}

        if not matched_sources:
            update_job_status(db_path, job_id, "FALLIDO")
            return {"error": f"Repository '{repo_name}' not found in Jules sources.", "task_id": job_id, "status": "FALLIDO"}
        elif len(matched_sources) > 1:
            update_job_status(db_path, job_id, "FALLIDO")
            return {"error": f"Repository '{repo_name}' matches multiple sources ({', '.join(matched_sources)}). Please use canonical owner/repo name.", "task_id": job_id, "status": "FALLIDO"}

        source_name = matched_sources[0]

        # Phase 2: Create the session
        sessions_url = f"{app_config.jules_api_url}/sessions"
        payload = {
            "prompt": task_description,
            "sourceContext": {
                "source": source_name,
                "githubRepoContext": {
                    "startingBranch": "main"
                }
            }
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

        try:
            with urllib.request.urlopen(req_sessions, timeout=30) as resp:
                session_data = json.loads(resp.read().decode("utf-8"))
                remote_session_id = session_data.get("name") or session_data.get("id")
                remote_state = session_data.get("state", "QUEUED")

                if remote_session_id:
                    try:
                        update_job_remote_id(db_path, job_id, remote_session_id, remote_state)
                    except Exception as e:
                        # Persistence failed, but remote session exists!
                        return {
                            "error": "Session submitted but failed to record locally. Use jules_agent_job_id to track it manually.",
                            "task_id": job_id,
                            "jules_agent_job_id": remote_session_id,
                            "status": "ERROR"
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
            # Definite rejection vs uncertain execution
            if e.code in (400, 401, 403, 404, 409, 422):
                update_job_status(db_path, job_id, "FALLIDO")
                return {"error": f"Jules API rejected POST /sessions request with HTTP error {e.code}", "task_id": job_id, "status": "FALLIDO"}
            else:
                # 5xx might mean it was queued or failed after being recorded
                update_job_status(db_path, job_id, "DESCONOCIDO")
                return {"error": f"Jules API returned ambiguous HTTP error {e.code} for POST /sessions", "task_id": job_id, "status": "DESCONOCIDO"}
        except Exception as e:
            # Timeout or other network error AFTER we potentially sent the request.
            # Outcome is uncertain. Do NOT automatically retry.
            update_job_status(db_path, job_id, "DESCONOCIDO")

            # Redact API key from generic exception string before truncation
            error_str = str(e)
            if app_config.jules_api_key in error_str:
                error_str = error_str.replace(app_config.jules_api_key, "***REDACTED***")
            error_str = error_str[:100]

            return {"error": f"Failed to contact Jules API or connection timed out: {error_str}", "task_id": job_id, "status": "DESCONOCIDO"}

    @mcp.tool()
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
