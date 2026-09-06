"""债#10 (2026-09-06): recall 读路径锁态 fail-open — B 即时路加固验收。

场景: autodream 蒸馏长写事务持锁 (合法写者), recall 即时 boost 写回
(``scoring.refresh_lif_on_recall``) 撞 ``database is locked``。修复前
OperationalError 炸穿 recall; 修复后三段防御: MEM_DB_BUSY_TIMEOUT (db.py
显式 busy timeout) + MEM_BOOST_RETRIES×MEM_BOOST_BACKOFF 短退避重试 +
穷尽后 fail-open 降级纯读 (stderr WARN, 不污染 --json stdout)。

硬要求 (派发令 ref=debt-10): recall 任何锁态下不崩; 持锁连接模拟下
recall 不崩; 无锁正控不回归 (boost 写回照常)。

测试规范: def test_xxx() 供 pytest 收集; tmp_path db 全隔离 (绝不碰
data/memory.db); 锁 = 第二条裸连接 BEGIN IMMEDIATE + 真实写 (WAL 写锁);
env 小 timeout/短 backoff 保证测试毫秒级。env 先于 db.init 设置 → 本测
连接即按小 timeout 建连 (避免中途换连接的隐藏态)。
"""
import sqlite3
import threading

import db
import recall as recall_mod
import store


def _setup(tmp_path, name, monkeypatch, *, busy="0.1", retries="1",
           backoff="0.05"):
    """复位连接缓存 + tmp db + env 小 timeout (测试毫秒级)。"""
    monkeypatch.setenv("MEM_DB_BUSY_TIMEOUT", busy)
    monkeypatch.setenv("MEM_BOOST_RETRIES", retries)
    monkeypatch.setenv("MEM_BOOST_BACKOFF", backoff)
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
    finally 里 rollback+close 释放。check_same_thread=False: test 3 的
    释放走 threading.Timer。
    """
    conn = sqlite3.connect(str(db_path), timeout=5, check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS _lock_holder (x TEXT)")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO _lock_holder (x) VALUES ('held')")
    return conn


def test_recall_lock_failopen_no_crash(tmp_path, monkeypatch, capsys):
    """持锁全程 + 重试穷尽 → recall 不崩, 命中照返, stderr WARN 降级。"""
    _setup(tmp_path, "lock.db", monkeypatch)
    fid = _seed_fact()
    holder = _hold_write_lock(tmp_path / "lock.db")
    try:
        res = recall_mod.recall("rust", session_id="s1")  # 不得 raise
    finally:
        holder.rollback()
        holder.close()

    assert res, "fail-open 降级纯读仍须返回命中集 (等效 boost=False)"
    assert any(f["id"] == fid for f in res), "命中的 seed fact 应在返回里"
    err = capsys.readouterr().err
    assert "LIF 记账降级" in err and "fail-open" in err, (
        f"重试穷尽后应 stderr WARN 降级, got: {err!r}")
    assert "database is locked" in err, "WARN 应携带底层锁败原因"


def test_recall_boost_ok_without_lock(tmp_path, monkeypatch, capsys):
    """无锁正控: 加固不改默认语义 — boost 写回照常, 无 WARN。"""
    _setup(tmp_path, "clean.db", monkeypatch, retries="2")
    fid = _seed_fact()

    res = recall_mod.recall("rust", session_id="s1")

    assert res and any(f["id"] == fid for f in res)
    after = store.get_fact(fid)
    assert after["access_count"] == 1, (
        f"无锁 boost 应写回 access_count=1, got {after['access_count']}")
    assert "LIF 记账降级" not in capsys.readouterr().err


def test_recall_lock_retry_succeeds_after_release(tmp_path, monkeypatch, capsys):
    """持锁但退避窗口内释放 → 重试成功, boost 写回落库, 无降级 WARN。"""
    _setup(tmp_path, "release.db", monkeypatch)
    fid = _seed_fact()
    holder = _hold_write_lock(tmp_path / "release.db")
    # attempt1 (busy 0.1s) 败于 ~0.1s; backoff 0.05s; attempt2 起 ~0.15s,
    # busy handler 等到 0.25s — 0.2s 释放落在窗口内 (50ms 余量)。
    timer = threading.Timer(0.2, lambda: (holder.rollback(), holder.close()))
    timer.start()
    try:
        res = recall_mod.recall("rust", session_id="s1")
    finally:
        timer.join()
        try:
            holder.rollback()
            holder.close()
        except sqlite3.ProgrammingError:
            pass  # timer 已关闭

    assert res, "重试成功路径仍返回命中"
    after = store.get_fact(fid)
    assert after["access_count"] == 1, (
        f"锁释放后重试应写回 access_count=1, got {after['access_count']}")
    assert "LIF 记账降级" not in capsys.readouterr().err
