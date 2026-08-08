"""ADR-2 ① entity UNIQUE(name, entity_type) 约束自验证.

覆盖: schema 约束生效 / db.init 老库迁移建 unique index / put_entity 冲突 fallback
find_entity_exact 复用既有行(与 resolver 两步闸语义一致, 不建孤儿)。

测试规范: def test_xxx() 函数让 pytest 收集(本项目头号雷区=模块级裸 assert 死代码)。
"""
import sqlite3
import tempfile
from pathlib import Path

import db
import store


def _fresh_db():
    """每个用例独立 tmp db, 不污染 data/memory.db。返回 (tmpdir, tmppath)。"""
    tmpdir = tempfile.mkdtemp()
    tmppath = Path(tmpdir) / "mem.db"
    db.init(tmppath)  # 强制重建连接到 tmp(含 UNIQUE 约束 + unique index 迁移)
    return tmpdir, tmppath


def test_unique_constraint_blocks_duplicate():
    """DB 层 UNIQUE(name, entity_type) 约束: 第二次 INSERT 同 name+type 必抛 IntegrityError。"""
    tmpdir, _ = _fresh_db()
    try:
        store.put_entity("Rust", "tool")
        # 直接走 store.put_entity 走 fallback 不抛, 故绕过 fallback 直查 DB 约束:
        conn = db.get_conn()
        with _expect_integrity_error(conn):
            conn.execute(
                "INSERT INTO entity (id, name, entity_type, properties, aliases, name_embedding, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("dup-id", "Rust", "tool", "{}", "[]", "[]", "2026-01-01T00:00:00+00:00"),
            )
    finally:
        import shutil
        shutil.rmtree(tmpdir)


def test_different_type_same_name_allowed():
    """UNIQUE(name, entity_type) 仅 (name,type) 全同才冲突: 同 name 不同 type 允许共存。"""
    tmpdir, _ = _fresh_db()
    try:
        eid_a = store.put_entity("Rust", "tool")
        eid_b = store.put_entity("Rust", "person")  # 不同 type → 不冲突
        assert eid_a != eid_b, "不同 entity_type 的同 name 应各建独立行"
        assert store.count_entities() == 2
    finally:
        import shutil
        shutil.rmtree(tmpdir)


def test_put_entity_conflict_fallback_reuses_existing():
    """put_entity 同 (name, entity_type) 冲突 → fallback find_entity_exact 复用既有 id, 不建孤儿。"""
    tmpdir, _ = _fresh_db()
    try:
        first = store.put_entity("Rust", "tool")
        # 第二次同 (name, type) — 约束冲突 → fallback 复用 first, 不建新行。
        second = store.put_entity("Rust", "tool")
        assert second == first, f"冲突 fallback 应返回既有 id {first}, got {second}"
        assert store.count_entities() == 1, "fallback 不应建孤儿行"
    finally:
        import shutil
        shutil.rmtree(tmpdir)


def test_put_entity_fallback_case_insensitive_match():
    """fallback find_entity_exact case-insensitive: 'Rust' 行存在时再插 'rust' 应复用。

    UNIQUE 约束是 case-sensitive(TEXT 默认), 故 'rust' != 'Rust' 不会触发 IntegrityError。
    此测验证: 严格 case 不同时 put_entity 正常建新行(约束不拦), 与 fallback 无关 —
    即 fallback 只在 exact-case 同 (name,type) 冲突时触发, 不误并 case 变体(那是 resolver
    step1 的职责)。
    """
    tmpdir, _ = _fresh_db()
    try:
        eid_upper = store.put_entity("Rust", "tool")
        # case 不同 → 约束(case-sensitive)不拦 → 正常建新行, 不走 fallback。
        eid_lower = store.put_entity("rust", "tool")
        assert eid_upper != eid_lower, "case 不同应各建独立行(UNIQUE 是 case-sensitive)"
        assert store.count_entities() == 2
    finally:
        import shutil
        shutil.rmtree(tmpdir)


def test_put_entity_fallback_preserves_explicit_entity_id_collision():
    """显式传 entity_id + 同 (name,type) 冲突 → fallback 仍复用既有行(忽略显式 id)。"""
    tmpdir, _ = _fresh_db()
    try:
        first = store.put_entity("Go", "tool")
        # 显式传一个新 entity_id, 但 (name,type) 冲突 → fallback 复用 first。
        second = store.put_entity("Go", "tool", entity_id="custom-id-123")
        assert second == first, "冲突时 fallback 应返回既有 id, 忽略显式 entity_id"
        # custom-id-123 不应作为孤儿行留下
        assert store.get_entity("custom-id-123") is None
    finally:
        import shutil
        shutil.rmtree(tmpdir)


def test_db_init_migration_creates_unique_index_on_legacy_db():
    """老库(无 UNIQUE 约束的 entity 表)→ db.init 建唯一索引 idx_entity_name_type。"""
    tmpdir = tempfile.mkdtemp()
    tmppath = Path(tmpdir) / "legacy.db"
    try:
        # 造一个"老库": 手建无 UNIQUE 约束的 entity 表(模拟 ITERATION_BASE 前的 schema)。
        conn = sqlite3.connect(str(tmppath))
        conn.execute(
            "CREATE TABLE entity (id TEXT PRIMARY KEY, name TEXT NOT NULL, entity_type TEXT NOT NULL, "
            "properties TEXT NOT NULL DEFAULT '{}', aliases TEXT NOT NULL DEFAULT '[]', "
            "name_embedding TEXT, created_at TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()
        # db.init 迁移: 应建 idx_entity_name_type unique index。
        db.init(tmppath)
        c2 = db.get_conn()
        idx_rows = c2.execute("PRAGMA index_list('entity')").fetchall()
        # PRAGMA index_list 列: seq,name,unique,origin,partial
        target = [r for r in idx_rows if r["name"] == "idx_entity_name_type"]
        assert len(target) == 1, f"迁移应建 idx_entity_name_type, got {[dict(r) for r in idx_rows]}"
        assert target[0]["unique"] == 1, f"index 应为 unique, got {dict(target[0])}"
    finally:
        import shutil
        shutil.rmtree(tmpdir)


def test_db_init_migration_idempotent_unique_index():
    """重复 db.init 不报错(unique index IF NOT EXISTS 幂等)。"""
    tmpdir, tmppath = _fresh_db()
    try:
        # 第二次 init 同路径 → cached 连接; 但即便重建, CREATE UNIQUE INDEX IF NOT
        # EXISTS 也不报已存在。
        db.init(tmppath)
        # 重复 init 后约束 + fallback 仍应工作
        eid1 = store.put_entity("Java", "tool")
        eid2 = store.put_entity("Java", "tool")
        assert eid1 == eid2, "重复 init 后约束 + fallback 仍应工作"
    finally:
        import shutil
        shutil.rmtree(tmpdir)


# ── helpers ─────────────────────────────────────────────────────────
class _expect_integrity_error:
    """assertRaises 风格: 断言 with 块抛 sqlite3.IntegrityError。"""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            raise AssertionError(
                f"Expected sqlite3.IntegrityError but INSERT succeeded on conn {self.conn}"
            )
        if not issubclass(exc_type, sqlite3.IntegrityError):
            return False  # 让非 IntegrityError 异常继续上抛
        return True  # 吞掉 IntegrityError(断言通过)
