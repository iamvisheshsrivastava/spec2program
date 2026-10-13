"""SQLite persistence for generated programs, validation, and recoveries.

Issue #16: every run of the pipeline - a generation, a batch, a runtime
recovery - previously vanished the moment the HTTP response was sent. There
was no audit trail: no way to look back at what was generated for a given
vehicle, whether it validated, or what recovery was issued for a given
failure. This module adds a small, dependency-free (stdlib ``sqlite3``)
history store and a minimal schema-migration mechanism, so the project has a
genuine persistence layer without pulling in a database server for a
single-file demo-scale app.

Design notes:
- SQLite is the right tool here: single-writer-friendly, zero ops, ships as
  one file under ``data/``, and the write volume (one row per API call) is
  nowhere near what would need a client/server database.
- A tiny ``schema_version`` table plus a list of forward-only migrations
  gives real "migration/schema" support without a heavyweight framework -
  appropriate for this project's size, and easy to extend later.
- Writes are best-effort: a persistence failure must never break the
  underlying API response (the whole point of this project is the
  generation/validation/recovery result, not the audit log of it), so
  callers should wrap ``record_*`` calls and log-and-continue on failure.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent

# sqlite3 connections are not thread-safe for concurrent use from multiple
# threads without care; serialise access with a single lock. Write volume
# here is low (one row per API call) so this is not a throughput concern,
# and `/api/batch` already fans out LLM calls, not database writes.
_lock = threading.Lock()
_connection: sqlite3.Connection | None = None

# Forward-only migrations, applied in order. Each is a tuple of SQL
# statements run once a database reaches that index (`schema_version.version`
# holds the number of migrations already applied).
_MIGRATIONS: list[tuple[str, ...]] = [
    (
        """
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            kind TEXT NOT NULL CHECK (kind IN ('generate', 'batch_item', 'recover')),
            vehicle_id TEXT,
            provider TEXT,
            is_valid INTEGER,
            request_json TEXT NOT NULL,
            response_json TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_history_vehicle_id ON history(vehicle_id)",
        "CREATE INDEX IF NOT EXISTS idx_history_kind ON history(kind)",
        "CREATE INDEX IF NOT EXISTS idx_history_created_at ON history(created_at)",
    ),
]


def _resolve_db_path() -> Path:
    path = Path(settings.database_path)
    if not path.is_absolute():
        path = BASE_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply any pending migrations, each as a single all-or-nothing step.

    Issue #28: Python's ``sqlite3`` module autocommits DDL statements (there
    is no implicit ``BEGIN`` before ``CREATE TABLE``/``CREATE INDEX`` the way
    there is for INSERT/UPDATE/DELETE), so running a migration's statements
    with plain ``conn.execute(...)`` calls and only committing at the very
    end meant a migration that failed partway through left its earlier DDL
    already applied to disk while ``schema_version`` stayed unbumped - a
    crash-unsafe partial state that would be retried (and fail again, e.g.
    on a non-idempotent statement) on every subsequent startup.
    Explicitly opening a transaction with ``BEGIN`` before each migration's
    statements makes SQLite treat the whole migration (DDL included) as one
    unit: either every statement plus the ``schema_version`` bump commits
    together, or a failure rolls all of it back, leaving the database
    exactly as it was before that migration was attempted.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
    )
    conn.commit()
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    current = row[0] if row else 0
    for version, statements in enumerate(_MIGRATIONS[current:], start=current + 1):
        try:
            conn.execute("BEGIN")
            for statement in statements:
                conn.execute(statement)
            if row:
                conn.execute("UPDATE schema_version SET version = ?", (version,))
            else:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
                row = (version,)  # subsequent iterations update instead of insert
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def get_connection() -> sqlite3.Connection:
    """Return the process-wide SQLite connection, creating/migrating it on first use."""
    global _connection
    with _lock:
        if _connection is None:
            db_path = _resolve_db_path()
            _connection = sqlite3.connect(db_path, check_same_thread=False)
            _connection.row_factory = sqlite3.Row
            _migrate(_connection)
            logger.info("Opened history database at %s", db_path)
        return _connection


def reset_connection() -> None:
    """Close and forget the cached connection (tests use this with a temp DB path)."""
    global _connection
    with _lock:
        if _connection is not None:
            _connection.close()
            _connection = None


@contextmanager
def _cursor():
    conn = get_connection()
    with _lock:
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def record_history(
    *,
    kind: str,
    vehicle_id: str | None,
    provider: str | None,
    is_valid: bool | None,
    request_json: str,
    response_json: str,
) -> int:
    """Insert one history row and return its id.

    Callers should treat persistence as best-effort: wrap this in
    try/except and log on failure rather than letting a database problem
    fail the underlying API call.
    """
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO history (kind, vehicle_id, provider, is_valid, request_json, response_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (kind, vehicle_id, provider, None if is_valid is None else int(is_valid),
             request_json, response_json),
        )
        return int(cur.lastrowid)


def list_history(
    *,
    kind: str | None = None,
    vehicle_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Return history rows (newest first), optionally filtered by kind/vehicle_id."""
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    clauses, params = [], []
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if vehicle_id:
        clauses.append("vehicle_id = ?")
        params.append(vehicle_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = get_connection()
    with _lock:
        rows = conn.execute(
            f"""
            SELECT id, created_at, kind, vehicle_id, provider, is_valid
            FROM history {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
    return [dict(row) for row in rows]


def get_history_item(item_id: int) -> dict | None:
    """Return the full record (including request/response JSON) for one history row."""
    conn = get_connection()
    with _lock:
        row = conn.execute("SELECT * FROM history WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["request"] = json.loads(record.pop("request_json"))
    record["response"] = json.loads(record.pop("response_json"))
    return record
