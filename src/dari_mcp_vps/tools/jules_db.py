import sqlite3
import uuid
import datetime
from pathlib import Path
from typing import Any, Dict, Optional

def init_db(db_path: str) -> None:
    """Initialize the SQLite database and create the jules_jobs table if it doesn't exist."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS jules_jobs (
                id TEXT PRIMARY KEY,
                repo_name TEXT NOT NULL,
                task_description TEXT NOT NULL,
                jules_agent_job_id TEXT,
                status TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                remote_state TEXT
            )
        """)

        # Safely add the column if the table already existed without it
        try:
            cursor.execute("ALTER TABLE jules_jobs ADD COLUMN remote_state TEXT")
        except sqlite3.OperationalError:
            pass # Column likely already exists

        conn.commit()

def create_job(db_path: str, repo_name: str, task_description: str) -> str:
    """Create a new job in the database with status 'PENDIENTE'."""
    job_id = str(uuid.uuid4())
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO jules_jobs (id, repo_name, task_description, status, created_at, updated_at)
            VALUES (?, ?, ?, 'PENDIENTE', ?, ?)
        """, (job_id, repo_name, task_description, now, now))
        conn.commit()

    return job_id

def update_job_remote_id(db_path: str, job_id: str, remote_id: str) -> None:
    """Update a job with its remote Jules session ID and set status to 'EN_PROGRESO'."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE jules_jobs
            SET jules_agent_job_id = ?, status = 'EN_PROGRESO', updated_at = ?
            WHERE id = ?
        """, (remote_id, now, job_id))
        conn.commit()

def update_job_status(db_path: str, job_id: str, status: str, remote_state: Optional[str] = None) -> None:
    """Update a job status."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        if remote_state is not None:
            cursor.execute("""
                UPDATE jules_jobs
                SET status = ?, updated_at = ?, remote_state = ?
                WHERE id = ?
            """, (status, now, remote_state, job_id))
        else:
            cursor.execute("""
                UPDATE jules_jobs
                SET status = ?, updated_at = ?
                WHERE id = ?
            """, (status, now, job_id))
        conn.commit()

def get_job(db_path: str, job_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve a job by its ID."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM jules_jobs WHERE id = ?", (job_id,))
        row = cursor.fetchone()

        if row:
            return dict(row)
        return None
