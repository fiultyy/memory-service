"""G1 chunk 抽取管道测试 — mock laya_batch 契约 + 真 db (tmp_path fixture,
先例 test_laya_filter.py)。测试规范: def test_xxx() 函数让 pytest 收集。"""
from pathlib import Path

import chunk_graph
import db
import laya_client
import store


def _setup(tmp_path, monkeypatch, available=True):
    db.init(Path(tmp_path) / "chunk-graph.db")
    calls = []

    def fake_batch(state, questions, timeout=30.0):
        calls.append((state, questions))
        return fake_batch.answers(state, questions)
    fake_batch.answers = lambda s, q: None
    monkeypatch.setattr(laya_client, "laya_batch", fake_batch)
    monkeypatch.setattr(laya_client, "laya_available", lambda: available)
    return calls


# ── split_units / pack_units (纯函数) ───────────────────────────────

def test_split_units_cjk_and_ascii_punct():
    t = "采纳斯普利特方案。分两步执行！Done. Next? 分号；换行\n第二段"
    us = chunk_graph.split_units(t)
    assert us == ["采纳斯普利特方案。", "分两步执行！", "Done.", "Next?",
                  "分号；", "换行", "第二段"]


def test_split_units_consecutive_punct_and_empty():
    assert chunk_graph.split_units("真的吗？？真的。") == ["真的吗？？", "真的。"]
    assert chunk_graph.split_units("") == []
    assert chunk_graph.split_units(" \n\n  ") == []


def test_pack_units_budget():
    units = ["字" * 40] * 10  # 每句 ~10 tokens
    packs = chunk_graph.pack_units(units, budget=25)
    assert all(len("".join(p)) // 4 <= 25 for p in packs)
    assert [u for p in packs for u in p] == units  # 保序不丢句


def test_pack_units_oversized_singleton():
    packs = chunk_graph.pack_units(["短句。", "x" * 100], budget=10)
    assert packs == [["短句。"], ["x" * 100]]


# ── aggregate_chunks (mock laya) ────────────────────────────────────

def _filter_answers(state, questions):
    # 逐句过滤: 噪声句 noul=0.0, 结论句 noul=0.8
    return {qid: {"noul": 0.0 if "寒暄" in state else 0.8} for qid in questions}


def test_aggregate_filters_noise_and_clusters(tmp_path, monkeypatch):
    calls = _setup(tmp_path, monkeypatch)
    pack = ["你好呀寒暄。", "采纳斯普利特方案。", "分两步执行。"]
    monkeypatch.setattr(chunk_graph, "_filter_units",
                        lambda units: ["采纳斯普利特方案。", "分两步执行。"])
    agg = {qid: {"choice": "1", "confidence": 0.9}
           for qid in ("agg_0",)}  # agg_0 → 1 聚合; agg_1 低置信孤立
    laya_client.laya_batch.answers = \
        lambda s, q: agg if "candidate units" in s else _filter_answers(s, q)
    out = chunk_graph.aggregate_chunks(pack)
    assert out == [{"text": "采纳斯普利特方案。分两步执行。",
                    "units": ["采纳斯普利特方案。", "分两步执行。"]}]


def test_filter_units_state_per_sentence(tmp_path, monkeypatch):
    """T3 教训锚: 过滤步 state 只含该句 (整批共享 state 会洗成常数), 低 noul 被滤。"""
    calls = _setup(tmp_path, monkeypatch)
    laya_client.laya_batch.answers = _filter_answers
    pack = ["你好呀寒暄。", "采纳斯普利特方案。"]
    assert chunk_graph._filter_units(pack) == ["采纳斯普利特方案。"]
    assert [s for s, q in calls] == pack  # 每句恰一次, state=该句原文
    assert all(list(q) == [f"flt_{i}"] for i, (s, q) in enumerate(calls))


def test_aggregate_malformed_answer_not_crash(tmp_path, monkeypatch):
    """键位守卫锚: choice 非法/缺 confidence/幻觉 id → 孤立成 chunk, 不炸。"""
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(chunk_graph, "_filter_units", lambda units: units)
    laya_client.laya_batch.answers = lambda s, q: {
        "agg_0": {"choice": "1"},               # 缺 confidence
        "agg_1": {"choice": "999", "confidence": 0.9},  # 幻觉 id
        "agg_2": "garbage",                     # 非 dict
        "agg_3": {"choice": "not-digit", "confidence": 0.9},
    }
    pack = ["甲。", "乙。", "丙。", "丁。"]
    out = chunk_graph.aggregate_chunks(pack)
    assert [c["units"] for c in out] == [["甲。"], ["乙。"], ["丙。"], ["丁。"]]


def test_aggregate_batch_none_returns_none(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(chunk_graph, "_filter_units", lambda units: units)
    laya_client.laya_batch.answers = lambda s, q: None
    assert chunk_graph.aggregate_chunks(["甲。", "乙。"]) is None


def test_aggregate_laya_down_returns_none(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, available=False)
    assert chunk_graph.aggregate_chunks(["甲。"]) is None


# ── summarize_chunk (mock laya) ─────────────────────────────────────

def test_summarize_choice_pick(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    laya_client.laya_batch.answers = \
        lambda s, q: {"sum": {"choice": "1", "confidence": 0.8}}
    text = "采纳斯普利特方案。本方案将分两步完整执行并落地全部细节。"
    assert chunk_graph.summarize_chunk(text) == text.split("。")[1] + "。"


def test_summarize_malformed_falls_back_none(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    laya_client.laya_batch.answers = lambda s, q: {"sum": {"noul": 0.9}}
    text = "甲句。乙句比甲句长得多得多得多。"
    assert chunk_graph.summarize_chunk(text) is None  # 调用方降级用首句


def test_summarize_short_chunk_no_laya_call(tmp_path, monkeypatch):
    calls = _setup(tmp_path, monkeypatch)
    laya_client.laya_batch.answers = lambda s, q: None
    assert chunk_graph.summarize_chunk("采纳斯普利特方案。") == "采纳斯普利特方案。"
    assert calls == []


# ── ingest_chunks (真 db) ───────────────────────────────────────────

def test_ingest_puts_fact_with_summary(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(chunk_graph, "summarize_chunk", lambda t, u=None: "结论句")
    monkeypatch.setattr(chunk_graph, "_mount_topic", lambda s, t: "ent_1")
    store.put_entity("挂载点", "topic", entity_id="ent_1")
    fids = chunk_graph.ingest_chunks(
        [{"text": "采纳斯普利特方案。分两步。", "units": ["采纳斯普利特方案。", "分两步。"]}],
        "/tmp/wd", session_id="sess_1")
    assert len(fids) == 1
    f = store.get_fact(fids[0])
    assert f["value"] == "结论句"
    assert f["predicate"] == "chunk_of"
    assert f["subject_id"] == "ent_1"
    assert f["source_refs"] == ["采纳斯普利特方案。", "分两步。"]
    assert f["source_cwd"] == "/tmp/wd"
    assert f["seen_sessions"] == ["sess_1"]


def test_ingest_new_placeholder_topic(tmp_path, monkeypatch):
    """无候选/不中 → put_entity 新建占位 topic, fact 挂其上。"""
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(chunk_graph, "summarize_chunk",
                        lambda t, u=None: t[:30])
    import embedding
    monkeypatch.setattr(embedding, "embed", lambda t, providers=None: [])
    fids = chunk_graph.ingest_chunks([{"text": "全新领域的结论句。", "units": ["全新领域的结论句。"]}], None)
    f = store.get_fact(fids[0])
    ent = store.find_entity_exact("全新领域的结论句。")
    assert ent is not None and f["subject_id"] == ent["id"]
    assert f["value"] == "全新领域的结论句。"
