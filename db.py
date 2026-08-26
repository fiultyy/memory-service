"""mem-service DB bootstrap — SQLite connection + schema init.

Single-file DB (ADR-2: easy backup, single-machine v1). idempotent init().
The connection is shared with the store module via :data:`_conn`.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
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
    conn = sqlite3.connect(str(path), check_same_thread=False,
                           isolation_level=None)
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
    # M4 migration: 老 db 无 upgrade_queue 表(wings 异步升级队列) → CREATE IF NOT EXISTS
    # (整表新增, 无 ALTER 需求; 重复 init 幂等)。
    conn.execute(
        """CREATE TABLE IF NOT EXISTS upgrade_queue (
            id              TEXT PRIMARY KEY,
            material_ref    TEXT NOT NULL UNIQUE,
            transcript_path TEXT,
            byte_offset     INTEGER,
            surprise        REAL,
            priority        REAL NOT NULL DEFAULT 0,
            status          TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','in_flight','done','failed','dead')),
            attempts        INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_uq_status_priority ON upgrade_queue(status, priority DESC)"
    )
    # M11 migration: upgrade_queue 补 material_text/material_prov (源不变式:
    # wings 升级的提取输入 = 入队时转写的素材原文, 永不读自家 KG 作提取输入)。
    uq_cols = {r[1] for r in conn.execute("PRAGMA table_info(upgrade_queue)")}
    if "material_text" not in uq_cols:
        conn.execute("ALTER TABLE upgrade_queue ADD COLUMN material_text TEXT")
    if "material_prov" not in uq_cols:
        conn.execute("ALTER TABLE upgrade_queue ADD COLUMN material_prov TEXT")
    # batch 13 migration (开放谓词, 用户裁决 2026-08-27): fact 补 raw_predicate
    # (LLM 原文谓词; predicate 列存聚类后 canonical) + predicate_registry
    # 谓词注册表 (canonical/计数/embedding — 近似度词频统计机制)。
    fact_cols = {r[1] for r in conn.execute("PRAGMA table_info(fact)")}
    if "raw_predicate" not in fact_cols:
        conn.execute("ALTER TABLE fact ADD COLUMN raw_predicate TEXT")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS predicate_registry (
               canonical TEXT PRIMARY KEY,
               count INTEGER NOT NULL DEFAULT 0,
               embedding TEXT NOT NULL,
               updated_at TEXT NOT NULL
           )""")
    # perf/vec-index: sqlite-vec **硬依赖** (用户裁决 2026-08-26: 无降级 —
    # 失败响亮 raise VecIndexError 含可行动诊断, 不静默回退)。建 vec_entity/
    # vec_fact 两张 vec0 虚拟表 (cosine 度量)。
    import vec_index
    vec_index.init_conn(conn)
    _conn = conn
    _conn_path = str(path)
    # 连接切换 (含首建) → store 派生缓存 (exact 字典/gaz 词典键代) 重置,
    # 防跨库陈旧索引 (增量注册写入旧库 id)。
    import store as _store_mod
    _store_mod._reset_derived_caches()
    return conn


def get_conn() -> sqlite3.Connection:
    """Return the active connection, initialising with the default path if needed."""
    if _conn is None:
        init()
    assert _conn is not None
    return _conn


_txn_depth = 0  # 重入防护: 嵌套 transaction() 只最外层 BEGIN/COMMIT


@contextmanager
def transaction():
    """批量写事务 (perf/vec-index: 消逐语句 fsync commit — sub10 profile
    561 次 commit 2.1s)。

    连接是 autocommit (isolation_level=None): 无事务时每语句即时落盘
    (与旧逐写 commit 语义等价); 批量入口 (autodream/init_memory) 包本
    上下文 — 全批共享一个事务, 收尾一次 commit。嵌套只计深度, 最外层
    收尾; ``conn.commit()`` 收尾而非 ``execute("COMMIT")`` — 若嵌套代码
    已提前 commit (活动事务已无), commit() 是 no-op 不炸。异常路径也
    commit (匹配旧逐语句持久语义 — 已执行语句不回滚)。
    """
    global _txn_depth
    conn = get_conn()
    started = False
    if _txn_depth == 0 and not conn.in_transaction:
        conn.execute("BEGIN")
        started = True
    _txn_depth += 1
    try:
        yield
    finally:
        _txn_depth -= 1
        if _txn_depth == 0 and started:
            conn.commit()
