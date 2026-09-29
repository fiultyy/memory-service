"""G2 migrate_chunk_reform 测试 — tmp db + mock laya (先例 test_chunk_graph.py)。"""
from pathlib import Path

import db
import laya_client
import migrate_chunk_reform as mig
import store


def _setup(tmp_path, monkeypatch, available=True):
    db.init(Path(tmp_path) / "mig.db")
    monkeypatch.setattr(laya_client, "laya_available", lambda: available)
    # 逐句过滤: 含"寒暄"判噪声; 聚簇全 None (每句孤立 chunk); 其余题默认 None
    def fake_batch(state, questions, timeout=30.0):
        if all(q.get("type") == "noul" for q in questions.values()):
            return {qid: {"noul": 0.0 if "寒暄" in state else 0.8}
                    for qid in questions}
        return {qid: None for qid in questions}  # choice 题全守卫不过 → 孤立
    monkeypatch.setattr(laya_client, "laya_batch", fake_batch)
    return fake_batch


def _seed(entities=3):
    ids = [store.put_entity(n, "topic") for n in ("甲主题", "乙主题", "丙主题")]
    fids = []
    for i in range(3):  # 甲: 3 fact (1 噪声 + 2 结论), topic 结论句口径
        topic = f"甲寒暄{i}。" if i == 0 else f"甲结论{i}。"
        fids.append(store.put_fact(ids[0], "p", f"obj{i}", topic=topic))
    fids.append(store.put_fact(ids[1], "p", "objB", topic="乙结论。"))      # 单 fact 组
    fids.append(store.put_fact(ids[2], "p", "objC", topic="丙寒暄噪声。"))  # 单 fact 全滤组
    return ids, fids


def test_plan_readonly(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    ids, fids = _seed()
    conn = db.get_conn()
    before = conn.execute(
        "select (select count(*) from fact), (select count(*) from entity)"
    ).fetchone()
    st = mig.plan(conn)
    after = conn.execute(
        "select (select count(*) from fact), (select count(*) from entity)"
    ).fetchone()
    assert before == after  # 干跑零写库
    assert st["total_facts"] == 5
    assert st["degraded_groups"] == 0
    # 4 chunk: 甲2 + 乙1 + 丙0 (全滤); filtered = 甲噪声 + 丙噪声 = 2
    assert st["chunks"] == 3
    assert st["filtered"] == 2
    assert st["delete_facts"] == 5
    assert st["orphan_entities_now"] == 0


def test_execute_wipes_old_and_keeps_chunks(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr("chunk_graph.summarize_chunk", lambda t, u=None: t[:30])
    ids, fids = _seed()
    monkeypatch.setattr("chunk_graph._mount_topic",
                        lambda s, t: ids[0] if s.startswith("甲") else ids[1])
    conn = db.get_conn()
    st = mig.plan(conn)
    with conn:
        mig.execute(conn, st)
    conn.commit()
    assert [f for f in (store.get_fact(i) for i in fids) if f] == []  # 旧 fact 零残留
    chunks = conn.execute(
        "select value, predicate, extractor, source_refs from fact "
        "where extractor='chunk_graph'").fetchall()
    assert len(chunks) == 3
    assert all(c[1] == "chunk_of" and c[0] for c in chunks)  # 有结论句
    import json
    refs = [json.loads(c[3]) for c in chunks]
    assert all(any(fid in s for r in refs for s in r) for fid in fids[:4])  # 甲乙溯源含旧 fact id (丙全滤无 chunk)
    # 丙主题全滤 → 无挂载 → 硬删; 甲乙被新 chunk 挂载或保留
    names = {r[0] for r in conn.execute("select name from entity")}
    assert "丙主题" not in names
    assert {"甲主题", "乙主题"} <= names


def test_execute_idempotent_refused(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr("chunk_graph.summarize_chunk", lambda t, u=None: t[:30])
    import embedding
    monkeypatch.setattr(embedding, "embed", lambda t, providers=None: [])
    ids, fids = _seed()
    conn = db.get_conn()
    st = mig.plan(conn)
    with conn:
        mig.execute(conn, st)
    conn.commit()
    st2 = mig.plan(conn)  # 二次计划 (此时库里已有 chunk fact)
    try:
        mig.execute(conn, st2)
        assert False, "二次 execute 应被拒绝"
    except SystemExit as e:
        assert "chunk_graph" in str(e)


def test_degraded_group_retried_not_lost(tmp_path, monkeypatch):
    """锚: 首轮 laya 批 None → 组降级; 重试轮成功 → 不计最终降级。"""
    _setup(tmp_path, monkeypatch)
    ids, _ = _seed()
    conn = db.get_conn()
    calls = {"n": 0}

    def flaky_batch(state, questions, timeout=30.0):
        if all(q.get("type") == "noul" for q in questions.values()):
            if "乙" in state and calls["n"] < 2:  # 乙组首轮过滤批 None
                calls["n"] += 1
                return None
            return {qid: {"noul": 0.8} for qid in questions}
        return {qid: None for qid in questions}
    monkeypatch.setattr(laya_client, "laya_batch", flaky_batch)
    monkeypatch.setattr(mig.time, "sleep", lambda s: None)  # 重试轮间不真等
    st = mig.plan(conn)
    assert st["degraded_groups"] == 0  # 重试轮救回
    assert any(gp["subject"] == "乙主题" for gp in st["group_plans"])
    assert calls["n"] == 2  # 确认首轮确实 None 过 (非误过)


def test_execute_survives_supersedes_fk_chain(tmp_path, monkeypatch):
    """锚: superseded 旧 fact 被 active 引用 (supersedes_id FK) 时删除不炸。"""
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr("chunk_graph.summarize_chunk", lambda t, u=None: t[:30])
    ids, fids = _seed()
    monkeypatch.setattr("chunk_graph._mount_topic", lambda s, t: ids[1])
    conn = db.get_conn()
    # 乙组再加一条 superseded 旧 fact, 被 active 乙结论引用
    old = store.put_fact(ids[1], "p", "旧值", topic="旧乙结论。")
    store.put_fact(ids[1], "p", "objB2", topic="乙结论2。",
                   supersedes_id=old, status="superseded")
    conn.execute("UPDATE fact SET status='superseded', valid_to='2026-01-01T00:00:00+00:00' "
                 "WHERE id = ?", (old,))
    conn.commit()
    st = mig.plan(conn)
    with conn:
        mig.execute(conn, st)
    conn.commit()
    n_old = conn.execute(
        "select count(*) from fact where extractor != 'chunk_graph'").fetchone()[0]
    assert n_old == 0  # 旧图 (含 superseded) 零残留


def test_snapshot_creates_backup(tmp_path, monkeypatch, capsys):
    db.init(Path(tmp_path) / "mig.db")
    src = db._conn_path
    dest = mig.snapshot(src)
    assert Path(dest).exists() and Path(dest).stat().st_size == Path(src).stat().st_size
    assert "md5=" in capsys.readouterr().out
