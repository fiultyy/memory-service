"""T4 · 语义边 + fact_relations schema 测试 (docs/specs/laya-integration-tickets.md T4)。

db.init(tmp) 隔离; discover 面 mock semantic_edges.laya_available/laya_batch
(from-import 绑定在 semantic_edges 命名空间)。延迟实测不做 (orchestrator 收口)。
"""
import tempfile
from pathlib import Path

import db
import laya_client  # noqa: F401  (确保同模块对象被 patch)
import recall as recall_mod
import semantic_edges
import store

t_early = "2026-01-01T00:00:00+00:00"
t0 = "2026-06-01T00:00:00+00:00"
t1 = "2026-07-01T00:00:00+00:00"
t_mid = "2026-06-15T00:00:00+00:00"
t_late = "2026-08-01T00:00:00+00:00"


def _answer(score: float) -> dict:
    return {"probabilities": {"0": 0.0, "1": 0.0, "2": 0.0, "3": 1.0},
            "score": score}


def _setup_pair(source_cwd=None, valid_from=None):
    """两条无共享实体的 fact (E1→E2, E3→E4)。返回 (fid1, fid2, eid)。"""
    e1 = store.put_entity("Alpha", "concept")
    e2 = store.put_entity("Bravo", "concept")
    e3 = store.put_entity("Charlie", "concept")
    e4 = store.put_entity("Delta", "concept")
    kw = dict(extractor="llm", fact_type="permanent", LIF=0.5, confidence=0.8,
              source_refs=["s"])
    if source_cwd:
        kw["source_cwd"] = source_cwd
    if valid_from:
        kw["valid_from"] = valid_from
    fid1 = store.put_fact(e1, "uses", "Alpha uses Bravo", object_id=e2, **kw)
    fid2 = store.put_fact(e3, "depends_on", "Charlie depends on Delta",
                          object_id=e4, **kw)
    return fid1, fid2, e1


# ── schema 幂等 ──────────────────────────────────────────────────

def test_schema_old_db_double_init_idempotent():
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "m.db")
    # 模拟老库: init 后删表 → 再连续两次 init 不炸且表复活
    db.get_conn().execute("DROP TABLE fact_relations")
    # 缓存连接短路绕过 executescript → 复位后重 init (test_m4_m9 先例)
    db._conn = None
    db._conn_path = None
    db.init(Path(tmp) / "m.db")
    db.init(Path(tmp) / "m.db")
    cols = [r[1] for r in db.get_conn().execute("PRAGMA table_info(fact_relations)")]
    assert cols == ["source_id", "target_id", "edge_type", "weight",
                    "created_by", "created_at"], cols
    idx = [r[1] for r in db.get_conn().execute(
        "PRAGMA index_list(fact_relations)")]
    assert any("idx_fact_relations_target" in (r[1] or "") for r in
               db.get_conn().execute("PRAGMA index_list(fact_relations)")), idx
    print("✓ 老库连续两次 init 幂等, 列/索引齐")


# ── put / get ────────────────────────────────────────────────────

def test_put_rewrite_updates_weight_no_dup():
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "m.db")
    fid1, fid2, _ = _setup_pair()
    n = store.put_semantic_edges(
        [{"source_id": fid1, "target_id": fid2, "weight": 0.7}])
    assert n == 1
    store.put_semantic_edges(
        [{"source_id": fid1, "target_id": fid2, "weight": 0.95}])
    rows = db.get_conn().execute(
        "SELECT weight, created_by, edge_type FROM fact_relations").fetchall()
    assert len(rows) == 1, f"同键重写不得重复行, got {len(rows)}"
    assert rows[0]["weight"] == 0.95
    assert rows[0]["created_by"] == "laya"
    assert rows[0]["edge_type"] == "semantic"
    edges = store.get_semantic_edges()
    assert len(edges) == 1 and edges[0]["weight"] == 0.95
    print("✓ put 同键重写: weight 更新, 不重复行")


def test_get_temporal_join_and_as_of():
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "m.db")
    fid1, fid2, _ = _setup_pair(valid_from=t0)
    store.put_semantic_edges(
        [{"source_id": fid1, "target_id": fid2, "weight": 0.8}])
    assert len(store.get_semantic_edges()) == 1, "两端 active 默认可见"

    # 任一端 valid_to 置值 (软删) → 边消失 (默认读)
    store.update_fact_status(fid2, "superseded", valid_to=t1)
    assert store.get_semantic_edges() == [], "target 端失效 → 边消失"

    # as_of 时间窗: t_mid 在 [t0, t1) 内 → 可见; t_late 在窗后 → 不可见
    assert len(store.get_semantic_edges(as_of=t_mid)) == 1, "as_of 窗内可见"
    assert store.get_semantic_edges(as_of=t_late) == [], "as_of 窗后不可见"
    assert store.get_semantic_edges(as_of=t_early) == [], "as_of 早于 valid_from 不可见"
    print("✓ get 时态: 软删隐藏 + as_of 三窗正确")


def test_get_source_cwd_filter():
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "m.db")
    fid1, fid2, _ = _setup_pair(source_cwd="/a")
    store.put_semantic_edges(
        [{"source_id": fid1, "target_id": fid2, "weight": 0.8}])
    assert len(store.get_semantic_edges(source_cwd="/a")) == 1
    assert store.get_semantic_edges(source_cwd="/b") == [], "cwd 不匹配两端 → 边隐藏"
    print("✓ source_cwd 过滤与 _build_entity_graph 同构")


# ── 实体图并入 ───────────────────────────────────────────────────

def test_graph_semantic_edge_bfs_reach_and_baseline():
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "m.db")
    fid1, fid2, e1 = _setup_pair()

    # 基线: 无语义边 → Charlie 翼 fact BFS 不可达 (hop=1)
    res = recall_mod.recall("Alpha", use_bfs=True, bfs_hops=1, boost=False)
    assert fid2 not in {f["id"] for f in res}, "无边时基线: Charlie 翼不可达"

    store.put_semantic_edges(
        [{"source_id": fid1, "target_id": fid2, "weight": 0.9}])
    res = recall_mod.recall("Alpha", use_bfs=True, bfs_hops=1, boost=False)
    assert fid2 in {f["id"] for f in res}, "语义边 → hop=1 可达 Charlie 翼 fact"
    print("✓ 图: 语义边 hop=1 可达; 无边时与基线一致")


# ── discover mock 面 ─────────────────────────────────────────────

def test_discover_mock_shape_threshold_and_none():
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "m.db")
    e1 = store.put_entity("Alpha", "concept")
    e2 = store.put_entity("Bravo", "concept")
    e3 = store.put_entity("Charlie", "concept")
    fid1 = store.put_fact(e1, "uses", "Alpha uses Bravo", object_id=e2,
                          extractor="llm", LIF=0.5, confidence=0.8)
    fid2 = store.put_fact(e2, "runs_on", "Bravo runs k8s", extractor="llm",
                          LIF=0.5, confidence=0.8)
    fid3 = store.put_fact(e3, "depends_on", "Charlie needs X", extractor="llm",
                          LIF=0.5, confidence=0.8)
    facts = {fid1: store.get_fact(fid1), fid2: store.get_fact(fid2)}
    nbrs = {fid1: [facts[fid2], store.get_fact(fid3)]}

    calls = []

    def fake_batch(state, questions, timeout=30.0):
        calls.append((state, questions))
        # fid2: score 3 → 3/3.0=1.0 ≥0.7 入边; fid3: score 1 → 0.33 <0.7 出局
        return {nid: _answer(3.0 if nid == fid2 else 1.0)
                for nid in questions}

    monkey_targets = semantic_edges
    orig_avail, orig_batch = monkey_targets.laya_available, monkey_targets.laya_batch
    monkey_targets.laya_available = lambda: True
    monkey_targets.laya_batch = fake_batch
    try:
        edges = semantic_edges.discover_semantic_edges([facts[fid1]], nbrs)
    finally:
        monkey_targets.laya_available, monkey_targets.laya_batch = orig_avail, orig_batch

    assert len(calls) == 1, "每 fact 恰 1 次 laya_batch"
    state, questions = calls[0]
    assert len(questions) == 2, "questions 数 == 邻居数"
    assert all(q["type"] == "score" and len(q["criteria"]) == 4
               for q in questions.values()), "4 档 score"
    assert edges == [{"source_id": fid1, "target_id": fid2,
                      "weight": 1.0, "created_by": "laya"}], edges

    # 批 None → 零边不炸
    monkey_targets.laya_batch = lambda s, q, timeout=30.0: None
    try:
        assert semantic_edges.discover_semantic_edges([facts[fid1]], nbrs) == []
    finally:
        monkey_targets.laya_batch = orig_batch

    # 开关关 (laya_available False) → 零调用零边
    monkey_targets.laya_available = lambda: False
    try:
        assert semantic_edges.discover_semantic_edges([facts[fid1]], nbrs) == []
    finally:
        monkey_targets.laya_available = orig_avail
    print("✓ discover: 1 fact 1 批 / questions==邻居 / 4档/3.0 / None→零边 / 关→零边")
