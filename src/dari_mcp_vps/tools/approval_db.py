import sqlite3
import uuid
import json
import datetime
from typing import Any, Dict, Optional, Tuple
from pathlib import Path
import hashlib
import secrets

def init_db(db_path: str) -> None:
    path = Path(db_path)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE NOT NULL,
                action TEXT NOT NULL,
                parameters TEXT NOT NULL,
                parameters_digest TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                capability_hash TEXT NOT NULL,
                result_diagnostic TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS approval_outbox (
                event_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                retries INTEGER DEFAULT 0,
                next_attempt TEXT,
                FOREIGN KEY (request_id) REFERENCES approvals(id)
            )
        """)

        conn.commit()

def create_request(
    db_path: str,
    idempotency_key: str,
    action: str,
    parameters: Dict[str, Any],
    app_secret: str,
    ttl_seconds: int = 300
) -> Tuple[str, str]:
    """Creates a new approval request or returns existing if idempotency key and digest match."""
    now = datetime.datetime.now(datetime.timezone.utc)
    expires_at = now + datetime.timedelta(seconds=ttl_seconds)

    if not isinstance(parameters, dict):
        raise ValueError("Parameters must be a dictionary")

    try:
        params_json = json.dumps(parameters, sort_keys=True)
        if "NaN" in params_json:
            raise ValueError("Parameters cannot contain NaN")
        if len(params_json) > 16384:
            raise ValueError("Parameters payload exceeds 16KB limit")
    except Exception as e:
        raise ValueError(f"Invalid parameters payload: {e}")

    # Bind action and parameters to the digest
    canonical_payload = json.dumps({
        "action": action,
        "parameters": parameters,
        "version": "1"
    }, sort_keys=True)

    params_digest = hashlib.sha256(canonical_payload.encode('utf-8')).hexdigest()

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        conn.execute("BEGIN EXCLUSIVE")
        # Check if exists
        cursor.execute("SELECT id, parameters_digest FROM approvals WHERE idempotency_key = ?", (idempotency_key,))
        row = cursor.fetchone()

        if row:
            existing_id, existing_digest = row
            if existing_digest != params_digest:
                conn.rollback()
                raise ValueError("Idempotency conflict: parameters or action have changed for the same key.")
            conn.rollback()
            return existing_id, "ALREADY_EXISTS"  # Token is lost but that's expected for existing requests (can't resend it)

        # Generate new (req_id max 12 chars to keep callback_data <= 64 bytes)
        req_id = f"a_{secrets.token_hex(4)}"

        import hmac
        capability_token = hmac.new(app_secret.encode('utf-8'), req_id.encode('utf-8'), hashlib.sha256).hexdigest()[:24]
        capability_hash = hashlib.sha256(capability_token.encode('utf-8')).hexdigest()

        cursor.execute("""
            INSERT INTO approvals (
                id, idempotency_key, action, parameters, parameters_digest,
                status, created_at, expires_at, capability_hash, result_diagnostic
            ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, NULL)
        """, (req_id, idempotency_key, action, params_json, params_digest, now.isoformat(), expires_at.isoformat(), capability_hash))

        # Insert a notification event
        event_id = str(uuid.uuid4())
        event_payload = json.dumps({
            "request_id": req_id,
            "action": action,
            "parameters": parameters,
            "parameters_digest": params_digest,
            "expires_at": expires_at.isoformat()
        })

        cursor.execute("""
            INSERT INTO approval_outbox (event_id, request_id, event_type, payload, status, created_at, retries, next_attempt)
            VALUES (?, ?, 'APPROVAL_REQUESTED', ?, 'PENDING', ?, 0, ?)
        """, (event_id, req_id, event_payload, now.isoformat(), now.isoformat()))

        conn.commit()

        return req_id, capability_token

def get_request(db_path: str, request_id: str) -> Optional[Dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM approvals WHERE id = ?", (request_id,))
        row = cursor.fetchone()
        if row:
            res = dict(row)
            res["parameters"] = json.loads(res["parameters"])
            if res["result_diagnostic"]:
                res["result_diagnostic"] = json.loads(res["result_diagnostic"])
            return res
        return None

def claim_decision(
    db_path: str,
    request_id: str,
    capability_token: str,
    decision: str
) -> bool:
    """Returns True if claimed successfully, False if invalid/expired/already claimed."""
    if decision not in ("APPROVED", "REJECTED"):
        raise ValueError("Invalid decision")

    now = datetime.datetime.now(datetime.timezone.utc)
    provided_hash = hashlib.sha256(capability_token.encode('utf-8')).hexdigest()

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, expires_at, capability_hash FROM approvals WHERE id = ?", (request_id,))
        row = cursor.fetchone()
        if not row:
            return False

        status, expires_at_str, capability_hash = row

        # Verify capability securely
        import hmac
        if not hmac.compare_digest(provided_hash.encode('utf-8'), capability_hash.encode('utf-8')):
            return False

        if status != "PENDING":
            return False

        expires_at = datetime.datetime.fromisoformat(expires_at_str)
        if now > expires_at:
            # We transition to EXPIRED below instead of claiming
            return False

        # Claim it
        cursor.execute("UPDATE approvals SET status = ? WHERE id = ? AND status = 'PENDING'", (decision, request_id))

        if cursor.rowcount > 0:
            # Record decision event
            event_id = str(uuid.uuid4())
            event_payload = json.dumps({
                "request_id": request_id,
                "decision": decision
            })
            cursor.execute("""
                INSERT INTO approval_outbox (event_id, request_id, event_type, payload, status, created_at)
                VALUES (?, ?, 'APPROVAL_DECIDED', ?, 'PENDING', ?)
            """, (event_id, request_id, event_payload, now.isoformat()))
            conn.commit()
            return True

        return False

def expire_pending_requests(db_path: str) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE approvals SET status = 'EXPIRED'
            WHERE status = 'PENDING' AND expires_at < ?
        """, (now,))

        conn.commit()

def transition_to_running(db_path: str) -> list[Dict[str, Any]]:
    """Returns a list of requests that were just moved to RUNNING_SIMULATION."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Atomically select and transition
        cursor.execute("UPDATE approvals SET status = 'RUNNING_SIMULATION' WHERE status = 'APPROVED' RETURNING id, action, parameters")

        approved = []
        for row in cursor.fetchall():
            req = dict(row)
            req["parameters"] = json.loads(req["parameters"])
            approved.append(req)

        conn.commit()
        return approved

def record_simulation_result(db_path: str, request_id: str, success: bool, diagnostic: Dict[str, Any]) -> None:
    new_status = "SIMULATED_SUCCESS" if success else "SIMULATED_FAILURE"
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE approvals
            SET status = ?, result_diagnostic = ?
            WHERE id = ? AND status = 'RUNNING_SIMULATION'
        """, (new_status, json.dumps(diagnostic), request_id))

        if cursor.rowcount > 0:
            event_id = str(uuid.uuid4())
            event_payload = json.dumps({
                "request_id": request_id,
                "status": new_status,
                "diagnostic": diagnostic
            })
            cursor.execute("""
                INSERT INTO approval_outbox (event_id, request_id, event_type, payload, status, created_at)
                VALUES (?, ?, 'SIMULATION_COMPLETED', ?, 'PENDING', ?)
            """, (event_id, request_id, event_payload, now))

        conn.commit()

def get_pending_outbox_events(db_path: str) -> list[Dict[str, Any]]:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM approval_outbox WHERE status = 'PENDING' AND (next_attempt IS NULL OR next_attempt <= ?) ORDER BY created_at ASC", (now,))
        return [dict(row) for row in cursor.fetchall()]

def mark_outbox_event_sent(db_path: str, event_id: str) -> None:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE approval_outbox SET status = 'SENT' WHERE event_id = ?", (event_id,))
        conn.commit()

def increment_outbox_retry(db_path: str, event_id: str, max_retries: int = 5) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT retries FROM approval_outbox WHERE event_id = ?", (event_id,))
        row = cursor.fetchone()
        if row:
            retries = row[0]
            if retries >= max_retries:
                cursor.execute("UPDATE approval_outbox SET status = 'FAILED' WHERE event_id = ?", (event_id,))
            else:
                backoff_seconds = 2 ** retries * 5
                next_attempt = (now + datetime.timedelta(seconds=backoff_seconds)).isoformat()
                cursor.execute("UPDATE approval_outbox SET retries = retries + 1, next_attempt = ? WHERE event_id = ?", (next_attempt, event_id))
        conn.commit()

def reset_stuck_simulations(db_path: str) -> None:
    # We cannot reliably know what a stuck simulation was doing, we just fail it closed for recovery
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE approvals SET status = 'SIMULATED_FAILURE', result_diagnostic = '{"error": "Simulation was interrupted by worker restart"}'
            WHERE status = 'RUNNING_SIMULATION'
        """)
        conn.commit()
