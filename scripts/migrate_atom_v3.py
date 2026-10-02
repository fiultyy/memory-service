"""atom 表 v3 迁移 (2026-10-02 泛化裁决): label 扩 preference/event + 三新列。

SQLite CHECK 不可 ALTER → 建新表 INSERT SELECT 保 id → 换名重建索引。
幂等: label CHECK 已含 preference → 跳过。FK 引用方 (atom_edge/tag_mount/
vec_atom) 引用 id 不变, PRAGMA foreign_keys=OFF 窗口内换表。

用法: python3 scripts/migrate_atom_v3.py [db_path]   (缺省 data/memory.db)
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

NEW_DDL = """CREATE TABLE atom (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    label TEXT NOT NULL
        CHECK(label IN ('fact','judgment','experience','summary',
                        'preference','event')),
    p_dur REAL DEFAULT 0.0,
    valid_from TEXT, valid_to TEXT,
    source_refs TEXT, source_cwd TEXT,
    subjects TEXT, event_at TEXT, last_seen_at TEXT,
    needs_embed INTEGER DEFAULT 0, needs_audit INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')))"""


def needs_migrate(conn: sqlite3.Connection) -> bool:
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='atom'"
    ).fetchone()
    return ddl is not None and "preference" not in (ddl[0] or "")


def migrate(db_path: str | Path) -> dict:
    import db
    conn = db.init(Path(db_path))
    if not needs_migrate(conn):
        return {"skipped": "already v3"}
    conn.execute("PRAGMA foreign_keys=OFF")
    # legacy_alter_table=ON: RENAME 不改写引用方 FK (缺省 OFF 会把
    # atom_edge/tag_mount 的 REFERENCES 改指 atom_old_v2 — 生产已踩, 勿再踩)
    conn.execute("PRAGMA legacy_alter_table=ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(atom)")]
        keep = [c for c in ("id", "text", "label", "p_dur", "valid_from",
                            "valid_to", "source_refs", "source_cwd",
                            "needs_embed", "needs_audit", "created_at")
                if c in cols]
        conn.execute("ALTER TABLE atom RENAME TO atom_old_v2")
        conn.execute(NEW_DDL)
        conn.execute(f"INSERT INTO atom({','.join(keep)}) "
                     f"SELECT {','.join(keep)} FROM atom_old_v2")
        conn.execute("DROP TABLE atom_old_v2")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_atom_valid "
                     "ON atom(valid_from, valid_to)")
        # last_seen_at 缺省 = valid_from (COALESCE 口径在查询侧, 此处物理回填)
        conn.execute("UPDATE atom SET last_seen_at=valid_from "
                     "WHERE last_seen_at IS NULL AND valid_from IS NOT NULL")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
    n = conn.execute("SELECT COUNT(*) FROM atom").fetchone()[0]
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    return {"migrated": n, "fk_violations": len(fk)}


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "data/memory.db"
    print(json.dumps(migrate(target), ensure_ascii=False))
