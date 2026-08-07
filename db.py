"""mem-service DB bootstrap — SQLite connection + schema init.

Single-file DB (ADR-2: easy backup, single-machine v1). idempotent init().
The connection is shared with the store module via :data:`_conn`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_DEFAULT_DB = Path(__file__).parent / "data" / "memory.db"

_conn: sqlite3.Connection | None = None
_conn_path: str | None = None


def init(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open (or reuse) the SQLite connection and ensure schema exists.

    Idempotent: repeated calls with the same path return the cached connection.
    Setting ``db_path`` switches the active connection (used by tests).
    """
    global _conn, _conn_path
    path = Path(db_path) if db_path else _DEFAULT_DB
    if _conn is not None and _conn_path == str(path):
        return _conn
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    # ADR-14 migration: 老 db fact 表无 source_cwd 列(b 方案 cwd 隔离)→ ALTER ADD。
    # CREATE TABLE IF NOT EXISTS 不改老表; PRAGMA table_info 检测 + ALTER 补列。
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fact)")}
    if "source_cwd" not in cols:
        conn.execute("ALTER TABLE fact ADD COLUMN source_cwd TEXT")
    conn.commit()
    _conn = conn
    _conn_path = str(path)
    return conn


def get_conn() -> sqlite3.Connection:
    """Return the active connection, initialising with the default path if needed."""
    if _conn is None:
        init()
    assert _conn is not None
    return _conn
