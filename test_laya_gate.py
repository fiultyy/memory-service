"""T1 · laya 批量 gate 测试 (docs/specs/laya-integration-tickets.md T1)。

mock laya_client.laya_batch; recall 级测试构图照 test_bfs_recall 先例:
A --uses--> B 链, query="Alpha" BFS 扩出 B 翼 fact。
"""
import tempfile
from pathlib import Path

import db
import gate
import laya_client
import recall as recall_mod
import store


def _answer(p2: float, score: float = 1.0) -> dict:
    return {"probabilities": {"0": 0.1, "1": round(1 - p2 - 0.1, 4), "2": p2},
            "score": score}


CANDS = {
    "f1": "Alpha uses Bravo",
    "f2": "Bravo runs on Alpha stack",
    "f3": "unrelated delta note",
}
ANCHORS = {"Alpha", "别名X"}


# ── run_gate_laya 单元面 ──────────────────────────────────────────

def test_batch_shape_single_call(monkeypatch):
    calls = []

    def fake_batch(state, questions, timeout=30.0):
        calls.append((state, questions))
        return {fid: _answer(0.9) for fid in questions}

    monkeypatch.setattr(laya_client, "laya_batch", fake_batch)
    v = gate.run_gate_laya(dict(CANDS), "Alpha", set(ANCHORS))
    assert len(calls) == 1, "恰 1 次 laya_batch 调用"
    state, questions = calls[0]
    assert len(questions) == len(CANDS), "questions 数 == 候选数"
    for fid, text in CANDS.items():
        assert f"[{fid}] {text}" in state, f"state 含候选行 {fid}"
        q = questions[fid]
        assert q["type"] == "score"
        assert q["criteria"] == ["low", "medium", "high"]
        assert f"[{fid}]" in q["instructions"]
    # f1/f2 锚上 Alpha, f3 锚不上 → keep False
    assert v["f1"]["keep"] is True
    assert v["f2"]["keep"] is True
    assert v["f3"]["keep"] is False
    assert v["f1"]["matched_anchor"] == "Alpha"


def test_threshold_two_sides(monkeypatch):
    monkeypatch.setattr(laya_client, "laya_batch",
                        lambda s, q, timeout=30.0: {fid: _answer(0.34)
                                                    for fid in q})
    v = gate.run_gate_laya({"f1": "Alpha thing"}, "Alpha", {"Alpha"})
    assert v["f1"]["keep"] is False, "P(high)=0.34 < 0.35 → 丢弃"
    monkeypatch.setattr(laya_client, "laya_batch",
                        lambda s, q, timeout=30.0: {fid: _answer(0.36)
                                                    for fid in q})
    v = gate.run_gate_laya({"f1": "Alpha thing"}, "Alpha", {"Alpha"})
    assert v["f1"]["keep"] is True, "P(high)=0.36 ≥ 0.35 → keep"


def test_match_score_half_and_bounded(monkeypatch):
    for score in (0.0, 1.0, 2.0):
        monkeypatch.setattr(laya_client, "laya_batch",
                            lambda s, q, timeout=30.0, sc=score: {
                                fid: _answer(0.9, sc) for fid in q})
        v = gate.run_gate_laya({"f1": "Alpha thing"}, "Alpha", {"Alpha"})
        assert v["f1"]["match_score"] == score / 2
        assert 0.0 <= v["f1"]["match_score"] <= 1.0


def test_anchor_semantics(monkeypatch):
    # keep 候选无 anchor 子串 → keep 翻 False
    monkeypatch.setattr(laya_client, "laya_batch",
                        lambda s, q, timeout=30.0: {fid: _answer(0.9)
                                                    for fid in q})
    v = gate.run_gate_laya({"f1": "totally different"}, "Alpha", {"Alpha"})
    assert v["f1"]["keep"] is False
    # 有 anchor → matched_anchor = 命中子串 (大小写不敏感)
    v = gate.run_gate_laya({"f1": "mentions ALPHA here"}, "Alpha", {"Alpha"})
    assert v["f1"]["keep"] is True
    assert v["f1"]["matched_anchor"] == "Alpha"


def test_batch_none_returns_none(monkeypatch):
    monkeypatch.setattr(laya_client, "laya_batch",
                        lambda s, q, timeout=30.0: None)
    assert gate.run_gate_laya(dict(CANDS), "Alpha", ANCHORS) is None


# ── recall 集成面 ────────────────────────────────────────────────

def _setup_graph():
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "mem.db")
    ea = store.put_entity("Alpha", "concept", aliases=["Alf"])
    eb = store.put_entity("Bravo", "concept")
    ec = store.put_entity("Charlie", "concept")
    fid_a = store.put_fact(ea, "uses", "Alpha uses Bravo", extractor="llm",
                           fact_type="permanent", LIF=0.5, confidence=0.8,
                           source_refs=["s"], topic="A uses B", object_id=eb)
    # B 翼: 两端 (Bravo/Charlie) 均非 query 命中实体, 值也不含 query token
    # (否则字面路径归 A 路), 仅 BFS 1-hop 经 Bravo 入场; 文本含 A 路实体
    # 别名 "Alf" → 锚得上
    fid_b = store.put_fact(eb, "runs_on", "Bravo runs on Alf stack",
                           extractor="llm", fact_type="permanent", LIF=0.5,
                           confidence=0.8, source_refs=["s"],
                           topic="B on A", object_id=ec)
    return fid_a, fid_b


def test_recall_laya_none_falls_back_to_run_gate(monkeypatch):
    _setup_graph()
    monkeypatch.setattr(laya_client, "laya_available", lambda: True)
    monkeypatch.setattr(laya_client, "laya_batch",
                        lambda s, q, timeout=30.0: None)
    rg_calls = []

    def fake_run_gate(cands, query, **kw):
        rg_calls.append(cands)
        raise gate.GateFailed("mock 不可用")

    monkeypatch.setattr(gate, "run_gate", fake_run_gate)
    res = recall_mod.recall("Alpha", use_bfs=True, bfs_hops=1, use_gate=True,
                            gate_provider=object(), boost=False)
    ids = {f["id"] for f in res}
    assert len(rg_calls) == 1, "laya 整批 None → 回落原 gate.run_gate"
    assert all(ids), "回落后 GateFailed → B 翼全不入, A 路保留"


def test_recall_laya_batch_none_b_wing_dropped(monkeypatch):
    """整批 None 且原 gate 也失败 → B 翼全不入、A 路保留 (降级语义锚)。"""
    fid_a, fid_b = _setup_graph()
    monkeypatch.setattr(laya_client, "laya_available", lambda: True)
    monkeypatch.setattr(laya_client, "laya_batch",
                        lambda s, q, timeout=30.0: None)
    monkeypatch.setattr(gate, "run_gate",
                        lambda c, qy, **kw: (_ for _ in ()).throw(
                            gate.GateFailed("断供")))
    res = recall_mod.recall("Alpha", use_bfs=True, bfs_hops=1, use_gate=True,
                            gate_provider=object(), boost=False)
    ids = {f["id"] for f in res}
    assert fid_a in ids, "A 路全保留"
    assert fid_b not in ids, "B 翼全不入"


def test_recall_laya_keep_accounts_match_score(monkeypatch):
    fid_a, fid_b = _setup_graph()
    monkeypatch.setattr(laya_client, "laya_available", lambda: True)
    monkeypatch.setattr(laya_client, "laya_batch",
                        lambda s, q, timeout=30.0: {
                            fid: _answer(0.9, 1.0) for fid in q})
    bumps = []
    monkeypatch.setattr(recall_mod.store, "bump_gate_score",
                        lambda fid, ms, conn=None: bumps.append((fid, ms)))
    # gate.run_gate 不应被触达
    monkeypatch.setattr(gate, "run_gate",
                        lambda c, qy, **kw: (_ for _ in ()).throw(
                            AssertionError("不应回落原 gate")))
    res = recall_mod.recall("Alpha", use_bfs=True, bfs_hops=1, use_gate=True,
                            gate_provider=object(), boost=False,
                            gate_account=True)
    by_id = {f["id"]: f for f in res}
    assert fid_b in by_id and by_id[fid_b].get("gate_keep") is True
    assert by_id[fid_b]["match_score"] == 0.5
    assert fid_a in by_id and "gate_keep" not in by_id[fid_a], "A 路不带 gate 键"
    assert (fid_b, 0.5) in bumps, "bump_gate_score 收到 match_score"


def test_env_off_original_path_untouched(monkeypatch):
    _setup_graph()
    monkeypatch.setenv("MEM_LAYA_ENABLED", "0")
    laya_client._avail_cache = None
    lb_calls = []
    monkeypatch.setattr(laya_client, "laya_batch",
                        lambda s, q, timeout=30.0: lb_calls.append(1) or {})
    assert laya_client.laya_available() is False

    def fake_run_gate(cands, query, **kw):
        return {fid: {"keep": True, "match_score": 0.8,
                      "matched_anchor": "Alpha"} for fid in cands}

    monkeypatch.setattr(gate, "run_gate", fake_run_gate)
    res = recall_mod.recall("Alpha", use_bfs=True, bfs_hops=1, use_gate=True,
                            gate_provider=object(), boost=False)
    assert res, "原路径照常出结果"
    assert not lb_calls, "MEM_LAYA_ENABLED=0 → laya_batch 零调用"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_malformed_answer_whole_batch_none(monkeypatch):
    """对抗全测确认项: 单条畸形 answer(缺 probabilities/score 非 数值) → 整批 None
    → 调用方回落原 run_gate, 不静默 B 翼全弃。"""
    monkeypatch.setenv("MEM_LAYA_ENABLED", "1")
    import laya_client as lc
    monkeypatch.setattr(
        lc, "laya_batch",
        lambda s, q, timeout=30.0: {"f0": {"score": 2.0,
                                           "probabilities": {"2": 0.9}},
                                    "f1": {"noul": 0.9}})  # f1 畸形
    import gate
    v = gate.run_gate_laya({"f0": "alpha uses beta", "f1": "gamma"},
                           "query", {"alpha"})
    assert v is None
