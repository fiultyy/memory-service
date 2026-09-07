"""债#10 (2026-09-06 修订): recall 读路径锁态 — 机制本样契约 (无回退)。

用户裁决「不要回退策略, 机制是怎样就怎样修」: 即时 boost 写回不跳过、
不降级 — 锁等待交给 SQLite busy handler (db.py MEM_DB_BUSY_TIMEOUT 显式
化); 霸锁根治在写侧 (autodream 长事务已按段拆短, 见
test_autodream_txn_scope)。真锁死 = 机制异常 → OperationalError 响亮
上抛, 绝不静默丢 LIF 强化写。

本文件锁定三个机制契约 (2026-09-07 MEM_DELAYED_REINFORCE 缺省翻转后):
1. 即时路径 (显式 env=0, _setup 统一 pin): 无锁 boost 写回照常; 锁死超
   busy timeout → OperationalError 响亮上抛, 不吞不降。
2. 改道缺省 (env 缺省=1): boost 零 DB 写 → 锁态下 recall 天然无争用,
   命中记 recall_hits 信号, 由 dream 批量补回 — 锁伤害永不及调用方。
3. 锁等待机制本体: db.transaction() BEGIN IMMEDIATE + MEM_DB_BUSY_TIMEOUT
   显式化 (见 test_db_txn_immediate / test_autodream_txn_scope)。

测试规范: def test_xxx() 供 pytest 收集; tmp_path db 全隔离 (绝不碰
data/memory.db); 锁 = 第二条裸连接 BEGIN IMMEDIATE + 真实写 (WAL 写锁)。
"""
import sqlite3

import db
import recall as recall_mod
import signals as signals_mod
import store
import pytest


def _setup(tmp_path, name, monkeypatch, busy="0.1"):
    """复位连接缓存 + tmp db + env 小 timeout (测试毫秒级)。

    MEM_DELAYED_REINFORCE 统一 pin "0" (即时写回路径): 本文件契约针对
    boost 写机制本体; 改道缺省行为由专项测试覆盖 (见文末)。
    """
    monkeypatch.setenv("MEM_DB_BUSY_TIMEOUT", busy)
    monkeypatch.setenv("MEM_DELAYED_REINFORCE", "0")
    db._conn = None  # 前序测试/套件可能缓存了别的连接
    db._conn_path = None
    db.init(tmp_path / name)


def _seed_fact(value: str = "rust", source_cwd: str = "/test") -> str:
    """造一条可被 recall('rust') 命中的 fact, 返回 fact_id。"""
    eid = store.put_entity("用户", "inferred")
    return store.put_fact(
        eid, "uses", value, extractor="llm", fact_type="permanent",
        source_cwd=source_cwd, LIF=0.6, confidence=0.8,
        source_refs=["session:s"], topic="用户使用 rust 开发")


def _hold_write_lock(db_path):
    """第二条裸连接 BEGIN IMMEDIATE + 真实写 → 持 WAL 写锁, 返回 conn。

    必须真实写一条 (光 BEGIN 不首写, busy handler 撞不上写锁); 调用方
    finally 里 rollback+close 释放。
    """
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.execute("CREATE TABLE IF NOT EXISTS _lock_holder (x TEXT)")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO _lock_holder (x) VALUES ('held')")
    return conn


def test_recall_boost_ok_without_lock(tmp_path, monkeypatch):
    """无锁正控: boost 写回照常 — 机制默认路径零变化。"""
    _setup(tmp_path, "clean.db", monkeypatch)
    fid = _seed_fact()

    res = recall_mod.recall("rust", session_id="s1")

    assert res and any(f["id"] == fid for f in res)
    assert store.get_fact(fid)["access_count"] == 1, (
        "无锁 boost 应写回 access_count=1")


def test_recall_stuck_lock_raises_loudly(tmp_path, monkeypatch):
    """锁死超 busy timeout → OperationalError 响亮上抛 (无回退契约)。"""
    db_path = tmp_path / "lock.db"
    _setup(tmp_path, "lock.db", monkeypatch)
    fid = _seed_fact()
    holder = _hold_write_lock(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            recall_mod.recall("rust", session_id="s1")
    finally:
        holder.rollback()
        holder.close()
    # 锁释放后机制自愈: 同库再 recall 正常写回。
    res = recall_mod.recall("rust", session_id="s1")
    assert any(f["id"] == fid for f in res)
    assert store.get_fact(fid)["access_count"] == 1


def test_delayed_default_under_lock_zero_db_write(tmp_path, monkeypatch):
    """改道缺省 (env=1) 锁态: recall 零 DB 写 → 无锁可争不 raise, 命中
    记 recall_hits 信号待 dream 补回 — 锁伤害永不及调用方 (M10 存在意义)。"""
    sig_dir = tmp_path / "sig"
    orig_dir = signals_mod._signals_dir
    signals_mod._signals_dir = lambda: sig_dir
    db_path = tmp_path / "lockd.db"
    _setup(tmp_path, "lockd.db", monkeypatch)
    monkeypatch.setenv("MEM_DELAYED_REINFORCE", "1")  # 改道缺省 (盖过 _setup pin)
    fid = _seed_fact()
    holder = _hold_write_lock(db_path)
    try:
        res = recall_mod.recall("rust", session_id="s1")  # 不得 raise
    finally:
        holder.rollback()
        holder.close()
        signals_mod._signals_dir = orig_dir
    assert any(f["id"] == fid for f in res)
    assert store.get_fact(fid)["access_count"] == 0, "改道模式锁态零 DB 写"
    lines = (sig_dir / "recall_hits.jsonl").read_text().splitlines()
    assert any(fid in l for l in lines if l.strip()), "命中应记入信号流待补回"
