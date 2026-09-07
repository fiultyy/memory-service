"""债#10 e2e 修订 (2026-09-07): 写意图事务 BEGIN IMMEDIATE — BUSY_SNAPSHOT 根治。

编排席三组对照 e2e 实录: 零竞争 PASS (102.4s +27facts), 真实竞争 FAIL —
autodream 1.0s 崩 consolidate.decay ``database is locked``。机制 = deferred
BEGIN 先读后写升级竞态: decay SELECT 全量 ~6.7k 建快照、循环末才首写,
窗口内任何并发提交推进 WAL → SQLITE_BUSY_SNAPSHOT (快照落后不可同事务
重试, busy timeout 对它不适用)。

修复: ``db.transaction()`` 缺省 BEGIN IMMEDIATE — 取锁先于读, 升级竞态
机制性不存在; 取锁等待本身是普通锁等待, busy timeout 生效 (等待方退避
而非死亡)。

本文件三条机制契约:
1. 病根钉死: deferred 读后写 + 并发提交 → 响亮即死 (确定性复现);
2. IMMEDIATE 锁先于读写存在: 事务未做任何写, 并发写者即被挡;
3. 取锁等待走 busy timeout: 持有者窗口内释放 → 成功取锁, 不炸。
"""
import sqlite3
import threading

import db
import pytest


def _reset_conn():
    db._conn = None
    db._conn_path = None


def test_deferred_readwrite_upgrade_snapshot_race(tmp_path):
    """病根钉死: deferred 事务先读后写, 并发提交推进 WAL → 即死不可救。

    这就是 e2e 里 consolidate.decay 的死法 — busy timeout 对
    BUSY_SNAPSHOT 不适用 (不是锁等待, 是快照落后), 只能机制预防。
    """
    db_path = tmp_path / "race.db"
    db.init(db_path)
    c1 = sqlite3.connect(str(db_path), timeout=5, isolation_level=None)
    c2 = sqlite3.connect(str(db_path), timeout=5, isolation_level=None)
    c1.execute("CREATE TABLE t (x TEXT)")
    c1.execute("INSERT INTO t VALUES ('a')")
    try:
        # deferred 事务: BEGIN 后先读 (此刻定快照)
        c1.execute("BEGIN")
        c1.execute("SELECT * FROM t").fetchall()
        # 并发写者提交 → WAL 推进越过 c1 的快照
        c2.execute("INSERT INTO t VALUES ('b')")
        c2.commit()
        # c1 首写: 升级撞过期快照 → BUSY_SNAPSHOT 响亮即死
        with pytest.raises(sqlite3.OperationalError):
            c1.execute("INSERT INTO t VALUES ('c')")
    finally:
        c1.rollback()
        c1.close()
        c2.close()


def test_transaction_immediate_lock_from_entry(tmp_path, monkeypatch):
    """IMMEDIATE: 取锁先于读 — 事务未做任何写, 并发写者已被挡在门外。"""
    db_path = tmp_path / "imm.db"
    monkeypatch.setenv("MEM_DB_BUSY_TIMEOUT", "5")
    _reset_conn()
    db.init(db_path)
    other = sqlite3.connect(str(db_path), timeout=0.2)
    other.execute("CREATE TABLE t (x TEXT)")
    try:
        with db.transaction():  # IMMEDIATE (缺省)
            assert db.get_conn().in_transaction
            # 本事务尚未写任何一行, 但写锁已在手 → 小超时写者即败
            with pytest.raises(sqlite3.OperationalError):
                other.execute("INSERT INTO t VALUES ('x')")
    finally:
        other.close()
        _reset_conn()


def test_transaction_immediate_waits_and_acquires(tmp_path, monkeypatch):
    """取锁等待走 busy timeout: 持有者窗口内释放 → 成功取锁不炸。"""
    db_path = tmp_path / "wait.db"
    monkeypatch.setenv("MEM_DB_BUSY_TIMEOUT", "1.0")
    _reset_conn()
    db.init(db_path)
    holder = sqlite3.connect(str(db_path), timeout=5, check_same_thread=False)
    holder.execute("CREATE TABLE t (x TEXT)")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO t VALUES ('held')")
    timer = threading.Timer(0.3, lambda: (holder.rollback(), holder.close()))
    timer.start()
    try:
        with db.transaction():  # 等 ~0.3s 后取锁成功
            db.get_conn().execute("INSERT INTO t VALUES ('got')")
    finally:
        timer.join()
        try:
            holder.rollback()
            holder.close()
        except sqlite3.ProgrammingError:
            pass  # timer 已关闭
        _reset_conn()
