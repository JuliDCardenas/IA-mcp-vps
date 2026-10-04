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

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS jules_events (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS jules_processed_activities (
                activity_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL
            )
        """)

        # Safely add the column if the table already existed without it
        try:
            cursor.execute("ALTER TABLE jules_jobs ADD COLUMN remote_state TEXT")
        except sqlite3.OperationalError:
            pass # Column likely already exists

        try:
            cursor.execute("ALTER TABLE jules_jobs ADD COLUMN followup_pending_since TIMESTAMP")
        except sqlite3.OperationalError:
            pass # Column likely already exists

        try:
            cursor.execute("ALTER TABLE jules_jobs ADD COLUMN activities_cursor TEXT")
        except sqlite3.OperationalError:
            pass # Column likely already exists

        conn.commit()

def get_active_jobs(db_path: str) -> list[Dict[str, Any]]:
    """Retrieve all jobs that need monitoring."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM jules_jobs WHERE (status IN ('EN_PROGRESO', 'PENDIENTE', 'ESPERANDO_FEEDBACK') OR followup_pending_since IS NOT NULL OR activities_cursor IS NOT NULL) AND jules_agent_job_id IS NOT NULL")
        return [dict(row) for row in cursor.fetchall()]

def is_activity_processed(db_path: str, activity_id: str) -> bool:
    """Check if an activity has already been processed."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM jules_processed_activities WHERE activity_id = ?", (activity_id,))
        return cursor.fetchone() is not None

def record_activity_processed(db_path: str, job_id: str, activity_id: str) -> None:
    """Record that an activity has been processed."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO jules_processed_activities (activity_id, job_id)
            VALUES (?, ?)
        """, (activity_id, job_id))
        conn.commit()

def set_followup_pending(db_path: str, job_id: str, is_pending: bool, pending_since_iso: Optional[str] = None) -> None:
    """Set or clear the followup_pending_since flag."""
    if is_pending:
        val = pending_since_iso if pending_since_iso else datetime.datetime.now(datetime.timezone.utc).isoformat()
    else:
        val = None

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE jules_jobs
            SET followup_pending_since = ?
            WHERE id = ?
        """, (val, job_id))
        conn.commit()

def record_event(db_path: str, job_id: str, event_type: str, payload_dict: Dict[str, Any]) -> None:
    """Record a webhook event for a job, injecting event_id and timestamp into the payload."""
    event_id = str(uuid.uuid4())
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    payload_dict["event_id"] = event_id
    payload_dict["timestamp"] = now

    import json
    payload_str = json.dumps(payload_dict)

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO jules_events (id, job_id, event_type, payload, status, created_at)
            VALUES (?, ?, ?, ?, 'PENDING', ?)
        """, (event_id, job_id, event_type, payload_str, now))
        conn.commit()

def record_activity_and_event(db_path: str, job_id: str, activity_id: str, event_type: Optional[str] = None, payload_dict: Optional[Dict[str, Any]] = None) -> None:
    """Record an activity as processed and optionally record a webhook event transactionally."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO jules_processed_activities (activity_id, job_id)
            VALUES (?, ?)
        """, (activity_id, job_id))

        # Only emit event if activity was actually inserted (not ignored)
        if cursor.rowcount > 0 and event_type and payload_dict is not None:
            event_id = str(uuid.uuid4())
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()

            payload_dict["event_id"] = event_id
            payload_dict["timestamp"] = now

            import json
            payload_str = json.dumps(payload_dict)

            cursor.execute("""
                INSERT INTO jules_events (id, job_id, event_type, payload, status, created_at)
                VALUES (?, ?, ?, ?, 'PENDING', ?)
            """, (event_id, job_id, event_type, payload_str, now))

        conn.commit()

def update_job_activities_cursor(db_path: str, job_id: str, cursor_token: Optional[str]) -> None:
    """Update the activities cursor for pagination."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE jules_jobs
            SET activities_cursor = ?
            WHERE id = ?
        """, (cursor_token, job_id))
        conn.commit()

def get_pending_events(db_path: str) -> list[Dict[str, Any]]:
    """Retrieve all pending events to dispatch."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM jules_events WHERE status = 'PENDING' ORDER BY created_at ASC")
        return [dict(row) for row in cursor.fetchall()]

def mark_event_sent(db_path: str, event_id: str) -> None:
    """Mark an event as successfully sent."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE jules_events SET status = 'SENT' WHERE id = ?", (event_id,))
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
