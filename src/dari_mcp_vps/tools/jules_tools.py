import json
import urllib.error
import urllib.request
from typing import Any

from dari_mcp_vps.tools.jules_db import init_db, create_job, update_job_remote_id, get_job, update_job_status


def register_jules_tools(mcp: Any, app_config: Any) -> None:
    db_path = app_config.jules_db_path
    init_db(db_path)

    @mcp.tool()
    def jules_request_coding_task(repo_name: str, task_description: str) -> dict[str, Any]:
        """Create a Jules coding session for a given repository and task."""
        if not app_config.jules_api_key:
            return {"error": "JULES_API_KEY is not configured", "status": "ERROR"}

        # Verify task description isn't empty
        if not task_description or not task_description.strip():
             return {"error": "task_description cannot be empty", "status": "ERROR"}

        job_id = create_job(db_path, repo_name, task_description)

        try:
            # First, fetch sources to resolve the repo_name to a source name
            sources_url = f"{app_config.jules_api_url}/sources"
            req_sources = urllib.request.Request(
                sources_url,
                method="GET",
                headers={"X-Goog-Api-Key": app_config.jules_api_key}
            )

            source_name = None
            with urllib.request.urlopen(req_sources, timeout=10) as resp:
                sources_data = json.loads(resp.read().decode("utf-8"))
                for source in sources_data.get("sources", []):
                    # Trying to find a source that matches the repo name.
                    # e.g., repo_name could be "IA-mcp-vps" and source name could be "sources/github/owner/IA-mcp-vps"
                    if source.get("name", "").endswith(f"/{repo_name}") or source.get("id", "").endswith(f"/{repo_name}"):
                        source_name = source.get("name")
                        break

            if not source_name:
                update_job_status(db_path, job_id, "FALLIDO")
                return {"error": f"Repository '{repo_name}' not found in Jules sources.", "task_id": job_id, "status": "FALLIDO"}

            # Now, create the session
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
                    update_job_status(db_path, job_id, "FALLIDO")
                    return {"error": "Invalid response from Jules API: missing session ID", "task_id": job_id, "status": "FALLIDO"}

        except urllib.error.HTTPError as e:
            update_job_status(db_path, job_id, "FALLIDO")
            try:
                error_body = e.read().decode('utf-8')
            except Exception:
                error_body = str(e)
            return {"error": f"Jules API HTTP error {e.code}: {error_body}", "task_id": job_id, "status": "FALLIDO"}
        except Exception as e:
            update_job_status(db_path, job_id, "FALLIDO")
            return {"error": f"Failed to contact Jules API: {str(e)}", "task_id": job_id, "status": "FALLIDO"}

    @mcp.tool()
    def jules_check_task_status(task_id: str) -> dict[str, Any]:
        """Check the status of a previously requested Jules coding task."""
        job = get_job(db_path, task_id)
        if not job:
            return {"error": f"Task ID {task_id} not found", "status": "NOT_FOUND"}

        return {
            "task_id": job["id"],
            "repo_name": job["repo_name"],
            "task_description": job["task_description"],
            "jules_agent_job_id": job["jules_agent_job_id"],
            "status": job["status"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"]
        }
