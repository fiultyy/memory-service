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
    # ADR-C migration: 老 db fact 表无 topic 列 → ALTER ADD。
    if "topic" not in cols:
        conn.execute("ALTER TABLE fact ADD COLUMN topic TEXT")
    # M1/M2/M3 migration (spec v2 schema 批): 老 db fact 表无 supersede_reason /
    # provenance / veracity 三列 → ALTER ADD。存量行不回填 (writer 不可考不臆测,
    # NULL=legacy — spec §1 v2·G11 默认); veracity 初值由 put_fact 按 provenance
    # 映射写入 (store.PROVENANCE_VERACITY), 老行无 provenance 亦不可考 → 同不回填。
    if "supersede_reason" not in cols:
        conn.execute("ALTER TABLE fact ADD COLUMN supersede_reason TEXT")
    if "provenance" not in cols:
        conn.execute("ALTER TABLE fact ADD COLUMN provenance TEXT")
    if "veracity" not in cols:
        conn.execute("ALTER TABLE fact ADD COLUMN veracity REAL")
    # ADR-D7 migration: 老 db entity 表无 aliases/name_embedding 列 → ALTER ADD。
    ent_cols = {r[1] for r in conn.execute("PRAGMA table_info(entity)")}
    if "aliases" not in ent_cols:
        conn.execute("ALTER TABLE entity ADD COLUMN aliases TEXT NOT NULL DEFAULT '[]'")
    if "name_embedding" not in ent_cols:
        conn.execute("ALTER TABLE entity ADD COLUMN name_embedding TEXT")
    # ADR-2 ① migration: 老 db entity 表无 UNIQUE(name, entity_type) 约束。
    # SQLite 不支持 ALTER ADD CONSTRAINT → 用 unique index 达成同等强制(老库空, 无冲突
    # 负担; 生产有冲突行会 CREATE UNIQUE INDEX 失败 → 记 P4: 先跑 consolidate dedup)。
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_name_type ON entity(name, entity_type)"
    )
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
