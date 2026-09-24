from __future__ import annotations

import base64
import json
import uuid
from typing import Any

from dari_mcp_vps.tools.coding_jobs import _exec, _validate_text_list

REPOSITORY_ALIAS = "repositorio_bd_emision"
SCRIPT = "/opt/agy-job/run-implementation-job.sh"


def register_private_coding_job_tool(mcp: Any, app_config: Any) -> None:
    @mcp.tool()
    def coding_private_job_create(
        goal: str,
        acceptance_criteria: list[str],
        constraints: list[str] | None = None,
        base_branch: str = "main",
    ) -> dict[str, Any]:
        """Create an isolated implementation job for the private emission repository."""
        if base_branch != "main":
            raise ValueError("Only base_branch main is allowed")
        goal = goal.strip()
        if not goal or len(goal) > 4000:
            raise ValueError("goal must contain 1-4000 characters")
        criteria = _validate_text_list("acceptance_criteria", acceptance_criteria)
        if not criteria:
            raise ValueError("At least one acceptance criterion is required")
        clean_constraints = _validate_text_list("constraints", constraints)
        job_id = f"job_{uuid.uuid4().hex}"
        request = {
            "job_id": job_id,
            "repository": REPOSITORY_ALIAS,
            "task_type": "implement",
            "goal": goal,
            "acceptance_criteria": criteria,
            "constraints": clean_constraints,
            "base_branch": base_branch,
        }
        encoded = base64.urlsafe_b64encode(json.dumps(request).encode("utf-8")).decode("ascii")
        _exec([SCRIPT, job_id, encoded], detach=True)
        return {
            "job_id": job_id,
            "status": "CREATED",
            "repository": REPOSITORY_ALIAS,
            "task_type": "implement",
        }
