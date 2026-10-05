import pytest
import sqlite3
import os
import shutil
import tempfile
import time
import subprocess
from pathlib import Path
from typing import Generator

from dari_mcp_vps.tools.jules_exporter import do_export

@pytest.fixture
def temp_env() -> Generator[tuple[Path, Path], None, None]:
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        source_db = tdp / "source.db"
        output_dir = tdp / "output"
        yield source_db, output_dir

def create_valid_source(db_path: Path):
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=DELETE;")
    conn.execute("""
        CREATE TABLE jules_jobs (
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
    conn.execute("""
        CREATE TABLE jules_events (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE jules_processed_activities (
            activity_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL
        )
    """)

    conn.execute("BEGIN TRANSACTION")
    conn.execute("INSERT INTO jules_jobs VALUES ('job1', 'repo1', 'secret_task1', 'agt1', 'PENDIENTE', '2023-01-01', '2023-01-02', 'REMOTE_ST')")
    conn.execute("INSERT INTO jules_jobs VALUES ('job2', 'repo2', 'secret_task2', NULL, 'COMPLETADO', '2023-01-03', '2023-01-04', NULL)")
    conn.commit()
    conn.close()

def test_export_successful(temp_env):
    source_db, output_dir = temp_env
    create_valid_source(source_db)

    do_export(str(source_db), str(output_dir))

    out_db = output_dir / "exported_jobs.db"
    assert out_db.exists()

    conn = sqlite3.connect(out_db)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Check jobs
    c.execute("SELECT * FROM jules_jobs ORDER BY id")
    jobs = [dict(r) for r in c.fetchall()]
    assert len(jobs) == 2

    # Verify whitelist (task_description shouldn't exist)
    assert "task_description" not in jobs[0]
    assert jobs[0]["id"] == "job1"
    assert jobs[0]["repo_name"] == "repo1"
    assert jobs[0]["status"] == "PENDIENTE"

    # Check metadata
    c.execute("SELECT * FROM export_metadata")
    meta = dict(c.fetchone())
    assert meta["error_code"] == "NONE"
    assert meta["last_success_at"] is not None

    conn.close()

def test_export_source_unavailable(temp_env):
    source_db, output_dir = temp_env
    # Don't create source

    do_export(str(source_db), str(output_dir))

    out_db = output_dir / "exported_jobs.db"
    assert out_db.exists()

    conn = sqlite3.connect(out_db)
    c = conn.cursor()
    c.execute("SELECT count(*) FROM jules_jobs")
    assert c.fetchone()[0] == 0

    c.execute("SELECT error_code FROM export_metadata")
    assert c.fetchone()[0] == "SOURCE_UNAVAILABLE"
    conn.close()

def test_export_invalid_schema(temp_env):
    source_db, output_dir = temp_env
    conn = sqlite3.connect(source_db)
    conn.execute("CREATE TABLE foo (bar INT)")
    conn.close()

    do_export(str(source_db), str(output_dir))

    out_db = output_dir / "exported_jobs.db"
    conn = sqlite3.connect(out_db)
    c = conn.cursor()
    c.execute("SELECT error_code FROM export_metadata")
    assert c.fetchone()[0] == "INVALID_SCHEMA"
    conn.close()

def test_export_path_collision(temp_env):
    source_db, output_dir = temp_env
    create_valid_source(source_db)

    # Output dir is the same as source DB's parent dir, and we rename source to exported_jobs.db
    # This shouldn't be allowed
    source_db.rename(source_db.parent / "exported_jobs.db")

    do_export(str(source_db.parent / "exported_jobs.db"), str(source_db.parent))

    # We should have rejected it and not touched it
    # We can verify by checking if export_metadata exists (it shouldn't if we aborted)
    conn = sqlite3.connect(str(source_db.parent / "exported_jobs.db"))
    c = conn.cursor()
    with pytest.raises(sqlite3.OperationalError):
        c.execute("SELECT * FROM export_metadata")
    conn.close()

def test_export_preserves_old_rows_on_error(temp_env):
    source_db, output_dir = temp_env
    create_valid_source(source_db)

    # 1. Successful export
    do_export(str(source_db), str(output_dir))

    # 2. Corrupt source
    os.remove(source_db)
    conn = sqlite3.connect(source_db)
    conn.execute("CREATE TABLE foo (bar INT)") # Invalid schema
    conn.close()

    # 3. Export again
    do_export(str(source_db), str(output_dir))

    # 4. Check rows are preserved and error updated
    out_db = output_dir / "exported_jobs.db"
    conn = sqlite3.connect(out_db)
    c = conn.cursor()

    c.execute("SELECT count(*) FROM jules_jobs")
    assert c.fetchone()[0] == 2 # Old rows preserved!

    c.execute("SELECT error_code FROM export_metadata")
    assert c.fetchone()[0] == "INVALID_SCHEMA"

    conn.close()

def test_export_hot_journal_crash_repro(temp_env):
    source_db, output_dir = temp_env

    # Create DB and seed with 500 records
    conn = sqlite3.connect(source_db, isolation_level=None)
    conn.execute('PRAGMA journal_mode=DELETE;')
    conn.execute('CREATE TABLE jules_jobs (id TEXT, status TEXT, task_description TEXT, repo_name TEXT, jules_agent_job_id TEXT, remote_state TEXT, created_at TEXT, updated_at TEXT)')
    conn.execute('BEGIN TRANSACTION;')
    for i in range(500):
        conn.execute('INSERT INTO jules_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                     (str(i), 'PENDIENTE', 'x'*4000, 'r', 'a', 'b', 'c', 'd'))
    conn.commit()
    conn.close()

    # Now simulate the writer crash that leaves a hot journal and a lock (via timeout script)
    script = f"""
import sqlite3
import os
conn = sqlite3.connect('{source_db}', timeout=5)
conn.execute('PRAGMA cache_size=5')
conn.execute('BEGIN IMMEDIATE')
conn.execute("UPDATE jules_jobs SET status='EN_CURSO_'")
os._exit(0)
"""
    subprocess.run(["python3", "-c", script])

    # Ensure hot journal exists
    assert (source_db.parent / (source_db.name + "-journal")).exists()

    # Do export. It should fail gracefully with SOURCE_BUSY
    do_export(str(source_db), str(output_dir))

    out_db = output_dir / "exported_jobs.db"
    assert out_db.exists()

    conn = sqlite3.connect(out_db)
    c = conn.cursor()

    c.execute("SELECT error_code FROM export_metadata")
    error_code = c.fetchone()[0]
    assert error_code == "SOURCE_BUSY"

    # Original should be untouched, let's recover it properly via writer
    conn_writer = sqlite3.connect(source_db)
    c_writer = conn_writer.cursor()
    c_writer.execute("SELECT count(*) FROM jules_jobs WHERE status='EN_CURSO_'")
    assert c_writer.fetchone()[0] == 0  # Recovered back to PENDIENTE
    conn_writer.close()
