import sqlite3
import os
import asyncio
import logging
import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

def do_export(source_db_path: str, output_dir: str):
    """
    Exports a sanitised version of the jules_jobs table to exported_jobs.db in output_dir.
    """
    output_path = Path(output_dir) / "exported_jobs.db"
    temp_path = Path(output_dir) / "exported_jobs.db.tmp"

    # 1) Safety Checks
    try:
        source_real = Path(source_db_path).resolve()
        output_dir_real = Path(output_dir).resolve()
        output_real = output_path.resolve()

        if source_real == output_real:
            logger.error("Source DB and output DB path collide! Aborting export.")
            return

        # Create output dir if it doesn't exist
        os.makedirs(output_dir_real, exist_ok=True)
    except Exception as e:
        logger.error(f"Error resolving paths for export: {e}")
        return

    error_code = "NONE"
    rows_to_export = []

    # 2) Read source (ro mode, isolated transaction)
    if not source_real.exists():
        error_code = "SOURCE_UNAVAILABLE"
    else:
        # Use query_only and ro mode to ensure we don't recover a hot journal
        uri = f"file:{source_real}?mode=ro&nolock=1" # Use nolock to read even if locked? No, contract says fail closed.
        uri = f"file:{source_real}?mode=ro"

        source_conn = None
        try:
            # We connect and try to read
            source_conn = sqlite3.connect(uri, uri=True, timeout=5.0)

            # Using PRAGMA query_only=1 to be double safe
            source_conn.execute("PRAGMA query_only=ON;")

            # Read rows
            source_conn.row_factory = sqlite3.Row
            cursor = source_conn.cursor()

            # Whitelist columns: id, repo_name, jules_agent_job_id, status, remote_state, created_at, updated_at
            # We must gracefully handle missing columns.
            try:
                cursor.execute("""
                    SELECT id, repo_name, jules_agent_job_id, status, remote_state, created_at, updated_at
                    FROM jules_jobs
                """)
                rows_to_export = [dict(row) for row in cursor.fetchall()]
            except sqlite3.OperationalError as e:
                # Column might be missing or table missing
                if "no such table" in str(e).lower():
                    # It's an empty DB or uninitialized DB
                    # The contract says: "El archivo publicado siempre tendrá ambas tablas: inicial sin fuente => tabla jobs vacía y metadata error, NO simular fuente vacía sana. Fuente legítimamente vacía => éxito NONE/0 filas"
                    # But if the table itself doesn't exist, it's either uninitialized or corrupted. Let's treat it as INVALID_SCHEMA
                    error_code = "INVALID_SCHEMA"
                elif "no such column" in str(e).lower():
                    error_code = "INVALID_SCHEMA"
                else:
                    raise e

        except sqlite3.OperationalError as e:
            if "attempt to write a readonly database" in str(e).lower():
                # This happens if there's a hot journal and ro mode attempts to recover it but fails
                error_code = "SOURCE_BUSY"
            elif "database is locked" in str(e).lower():
                error_code = "SOURCE_BUSY"
            else:
                logger.error(f"Operational error reading source DB: {e}")
                error_code = "EXPORT_ERROR"
        except Exception as e:
            logger.error(f"Unexpected error reading source DB: {e}")
            error_code = "EXPORT_ERROR"
        finally:
            if source_conn:
                try:
                    source_conn.close()
                except:
                    pass

    # 3) Write output safely to temporary file
    # If error_code != NONE, we should preserve old rows if we have an old output file
    if error_code != "NONE" and output_path.exists():
        old_uri = f"file:{output_path}?mode=ro"
        try:
            old_conn = sqlite3.connect(old_uri, uri=True)
            old_conn.row_factory = sqlite3.Row
            old_cursor = old_conn.cursor()
            old_cursor.execute("""
                SELECT id, repo_name, jules_agent_job_id, status, remote_state, created_at, updated_at
                FROM jules_jobs
            """)
            rows_to_export = [dict(row) for row in old_cursor.fetchall()]
            old_conn.close()
        except Exception as e:
            logger.warning(f"Could not read old output file to preserve rows: {e}")

    # Try to read old last_success_at
    last_success_at = None
    if output_path.exists():
        old_uri = f"file:{output_path}?mode=ro"
        try:
            old_conn = sqlite3.connect(old_uri, uri=True)
            old_conn.row_factory = sqlite3.Row
            old_cursor = old_conn.cursor()
            old_cursor.execute("SELECT last_success_at FROM export_metadata WHERE id=1")
            row = old_cursor.fetchone()
            if row and row["last_success_at"]:
                last_success_at = row["last_success_at"]
            old_conn.close()
        except Exception:
            pass

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if error_code == "NONE":
        last_success_at = now_iso

    try:
        # Create temp DB
        # Ensure we delete it first if it got left behind
        if temp_path.exists():
            os.remove(temp_path)

        temp_conn = sqlite3.connect(temp_path)

        # Schema definition (only whitelisted)
        temp_conn.execute("""
            CREATE TABLE jules_jobs (
                id TEXT PRIMARY KEY,
                repo_name TEXT NOT NULL,
                jules_agent_job_id TEXT,
                status TEXT NOT NULL,
                remote_state TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        temp_conn.execute("""
            CREATE TABLE export_metadata (
                id INTEGER PRIMARY KEY CHECK(id=1),
                last_attempt_at TEXT NOT NULL,
                last_success_at TEXT,
                error_code TEXT NOT NULL
            )
        """)

        # Insert rows
        if rows_to_export:
            temp_conn.executemany("""
                INSERT INTO jules_jobs (id, repo_name, jules_agent_job_id, status, remote_state, created_at, updated_at)
                VALUES (:id, :repo_name, :jules_agent_job_id, :status, :remote_state, :created_at, :updated_at)
            """, rows_to_export)

        # Insert metadata
        temp_conn.execute("""
            INSERT INTO export_metadata (id, last_attempt_at, last_success_at, error_code)
            VALUES (1, ?, ?, ?)
        """, (now_iso, last_success_at, error_code))

        temp_conn.commit()
        temp_conn.close()

        # 4) Atomic replace and permissions
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, output_path)

    except Exception as e:
        logger.error(f"Error writing export db: {e}")
        # Clean up temp file if something failed
        if temp_path.exists():
            try:
                os.remove(temp_path)
            except:
                pass


async def background_exporter(app_config):
    interval = app_config.jules_observability_interval
    if interval < 10:
        interval = 60 # Sanity check

    logger.info(f"Starting Jules background exporter (interval: {interval}s)")
    loop = asyncio.get_running_loop()

    while True:
        try:
            # Run in executor to avoid blocking the event loop
            await loop.run_in_executor(
                None,
                do_export,
                app_config.jules_db_path,
                app_config.jules_observability_output_dir
            )
        except Exception as e:
            logger.error(f"Unhandled error in background_exporter: {e}")

        await asyncio.sleep(interval)
