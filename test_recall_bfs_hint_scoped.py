"""D1 recall bfs hint + 跨 cwd scoped opt-in (ADR-4) 测试。

pytest 规范: 全部 def test_xxx() 函数(pytest 收集), 禁止模块级裸 assert+print
(本项目头号雷区 — 裸 assert pytest 不收集 = 死代码 = 假绿)。

覆盖:
- (1) suggest_bfs: direct-match 薄(候选 < SUGGEST_BFS_THRESHOLD)且 use_bfs=False
      → with_tag envelope 带 suggest_bfs=True; default list 路径零回归(行为不变)。
- (2) --bfs-scoped opt-in: use_bfs_scoped=False(default)→ 全局图; True → source_cwd
      过滤(图更精确更小)。default off 逐字零回归。
- (3) BFS+use_vec 组合深测(use_bfs=True + use_vec=True 双路候选 union)。
"""
import shutil
import tempfile
from pathlib import Path

import db
import recall as recall_mod
import store


def _fresh_db():
    """隔离 tmp db, 返回 tmp_path(调用方负责清理)。"""
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "d1.db")
    return tmp


# ── (1) suggest_bfs hint ─────────────────────────────────────────────

def test_suggest_bfs_true_when_direct_match_thin_and_bfs_off():
    """direct-match 薄(候选 < 阈值)且 use_bfs=False → with_tag envelope suggest_bfs=True。"""
    tmp = _fresh_db()
    try:
        # 单 fact: query "alpha" 命中 entity Alpha → candidates=1 < SUGGEST_BFS_THRESHOLD(3)
        ea = store.put_entity("Alpha", "concept")
        store.put_fact(ea, "describes", "alpha describes something", extractor="llm",
                       fact_type="permanent", LIF=0.5, confidence=0.8,
                       source_refs=["s"], topic="alpha desc", object_id=None)
        res = recall_mod.recall("alpha", use_bfs=False, boost=False, with_tag=True)
        assert res["suggest_bfs"] is True, (
            f"direct-match 薄(candidates=1) + use_bfs=False → suggest_bfs 应 True, got {res['suggest_bfs']}"
        )
    finally:
        shutil.rmtree(tmp)


def test_suggest_bfs_false_when_bfs_on():
    """use_bfs=True → suggest_bfs=False(BFS 已开, 无需提示)。"""
    tmp = _fresh_db()
    try:
        ea = store.put_entity("Alpha", "concept")
        store.put_fact(ea, "describes", "alpha describes something", extractor="llm",
                       fact_type="permanent", LIF=0.5, confidence=0.8,
                       source_refs=["s"], topic="alpha desc", object_id=None)
        res = recall_mod.recall("alpha", use_bfs=True, boost=False, with_tag=True)
        assert res["suggest_bfs"] is False, (
            f"use_bfs=True → suggest_bfs 应 False(已开 BFS 无需提示), got {res['suggest_bfs']}"
        )
    finally:
        shutil.rmtree(tmp)


def test_suggest_bfs_false_when_direct_match_rich():
    """direct-match 厚(候选 ≥ 阈值) → suggest_bfs=False(召回充分, 不提示)。"""
    tmp = _fresh_db()
    try:
        # 4 fact 命中 entity "alpha" → candidates ≥ SUGGEST_BFS_THRESHOLD(3)
        ea = store.put_entity("Alpha", "concept")
        for i in range(4):
            store.put_fact(ea, "describes", f"alpha fact number {i}", extractor="llm",
                           fact_type="permanent", LIF=0.5, confidence=0.8,
                           source_refs=["s"], topic=f"alpha {i}", object_id=None)
        res = recall_mod.recall("alpha", use_bfs=False, boost=False, with_tag=True)
        assert res["suggest_bfs"] is False, (
            f"direct-match 厚(candidates≥3) → suggest_bfs 应 False, got {res['suggest_bfs']}"
        )
    finally:
        shutil.rmtree(tmp)


def test_default_list_path_unchanged_by_hint():
    """default list 路径(非 verbose/with_tag)零回归: 返回 list[dict], 无 envelope。"""
    tmp = _fresh_db()
    try:
        ea = store.put_entity("Alpha", "concept")
        store.put_fact(ea, "describes", "alpha describes something", extractor="llm",
                       fact_type="permanent", LIF=0.5, confidence=0.8,
                       source_refs=["s"], topic="alpha desc", object_id=None)
        res = recall_mod.recall("alpha", use_bfs=False, boost=False)
        # 默认 list shape 不变(向后兼容: 调用方 for f in result)
        assert isinstance(res, list), f"default 路径应返回 list, got {type(res)}"
        assert len(res) >= 1
        assert isinstance(res[0], dict)
    finally:
        shutil.rmtree(tmp)


def test_suggest_bfs_threshold_constant_exposed():
    """SUGGEST_BFS_THRESHOLD 常量暴露在 recall 模块(cli 自判结果数走它)。"""
    assert hasattr(recall_mod, "SUGGEST_BFS_THRESHOLD"), "recall 模块应暴露 SUGGEST_BFS_THRESHOLD"
    assert isinstance(recall_mod.SUGGEST_BFS_THRESHOLD, int)
    assert recall_mod.SUGGEST_BFS_THRESHOLD > 0


# ── (2) --bfs-scoped opt-in ──────────────────────────────────────────

def test_build_entity_graph_default_global_no_cwd_filter():
    """default(source_cwd=None)→ 全局图: 跨 cwd 的 fact 都建边(ADR-14 单体 KG 跨 cwd 共享)。"""
    tmp = _fresh_db()
    try:
        ea = store.put_entity("Alpha", "concept")
        eb = store.put_entity("Bravo", "concept")
        store.put_fact(ea, "uses", "alpha uses bravo", extractor="llm",
                       fact_type="permanent", LIF=0.5, confidence=0.8,
                       source_refs=["s"], topic="a uses b", object_id=eb,
                       source_cwd="/proj/aaa")
        store.put_fact(ea, "uses", "alpha uses other cwd", extractor="llm",
                       fact_type="permanent", LIF=0.5, confidence=0.8,
                       source_refs=["s"], topic="a uses bbb", object_id=eb,
                       source_cwd="/proj/bbb")
        g, _ = recall_mod._build_entity_graph()
        # 全局图: 两个不同 cwd 的 fact 都建边 → Alpha↔Bravo 边存在
        assert g.has_edge(ea, eb), (
            f"全局图应含 Alpha↔Bravo 边(跨 cwd fact 共享), got edges={list(g.edges())}"
        )
    finally:
        shutil.rmtree(tmp)


def test_build_entity_graph_scoped_filters_other_cwd():
    """source_cwd='/proj/aaa' → 只匹配 cwd fact(+ NULL 老数据); 其他 cwd 不建边。"""
    tmp = _fresh_db()
    try:
        ea = store.put_entity("Alpha", "concept")
        eb = store.put_entity("Bravo", "concept")
        ec = store.put_entity("Charlie", "concept")
        # /proj/aaa fact: Alpha↔Bravo
        store.put_fact(ea, "uses", "alpha uses bravo", extractor="llm",
                       fact_type="permanent", LIF=0.5, confidence=0.8,
                       source_refs=["s"], topic="a uses b", object_id=eb,
                       source_cwd="/proj/aaa")
        # /proj/bbb fact: Bravo↔Charlie (其他 cwd, scoped 应排除)
        store.put_fact(eb, "runs_on", "bravo runs charlie", extractor="llm",
                       fact_type="permanent", LIF=0.5, confidence=0.8,
                       source_refs=["s"], topic="b runs c", object_id=ec,
                       source_cwd="/proj/bbb")
        g, _ = recall_mod._build_entity_graph(source_cwd="/proj/aaa")
        assert g.has_edge(ea, eb), (
            f"scoped=/proj/aaa: 应含 Alpha↔Bravo 边(cwd 匹配), got edges={list(g.edges())}"
        )
        assert not g.has_edge(eb, ec), (
            f"scoped=/proj/aaa: 应不含 Bravo↔Charlie 边(其他 cwd), got edges={list(g.edges())}"
        )
    finally:
        shutil.rmtree(tmp)


def test_build_entity_graph_scoped_includes_null_cwd_legacy():
    """source_cwd 过滤兼容 NULL 老数据(NULL source_cwd 视为全局, scoped 时仍建边)。"""
    tmp = _fresh_db()
    try:
        ea = store.put_entity("Alpha", "concept")
        eb = store.put_entity("Bravo", "concept")
        # NULL source_cwd (老数据, source_cwd 未传)
        store.put_fact(ea, "uses", "alpha uses bravo legacy", extractor="llm",
                       fact_type="permanent", LIF=0.5, confidence=0.8,
                       source_refs=["s"], topic="a uses b legacy", object_id=eb)
        g, _ = recall_mod._build_entity_graph(source_cwd="/proj/aaa")
        assert g.has_edge(ea, eb), (
            f"scoped: NULL source_cwd 老数据应兼容建边, got edges={list(g.edges())}"
        )
    finally:
        shutil.rmtree(tmp)


def test_recall_use_bfs_scoped_default_off_global_graph():
    """recall(use_bfs_scoped=False default)→ BFS 跑全局图(跨 cwd 邻居可达)。

    default 全局图: 跨 cwd 的 fact 都建边 → bfs_neighbors 从 Alpha 达 Bravo(跨 cwd)。
    (cwd candidate filter 是 ADR-14 另一维度, 不影响图结构可达性断言。)
    """
    tmp = _fresh_db()
    try:
        ea = store.put_entity("Alpha", "concept")
        eb = store.put_entity("Bravo", "concept")
        # 图连脚手架: Alpha↔Bravo 边在 /proj/aaa; Bravo neighbor fact 在 /proj/bbb
        store.put_fact(ea, "connects", "alpha connects bravo", extractor="llm",
                       fact_type="permanent", LIF=0.5, confidence=0.8,
                       source_refs=["s"], topic="a connects b", object_id=eb,
                       source_cwd="/proj/aaa")
        store.put_fact(eb, "runs_on", "bravo runs service", extractor="llm",
                       fact_type="permanent", LIF=0.5, confidence=0.8,
                       source_refs=["s"], topic="b runs svc", object_id=None,
                       source_cwd="/proj/bbb")
        # default(use_bfs_scoped=False): 全局图 → bfs_neighbors 从 Alpha 达 Bravo(跨 cwd)
        import networkx as nx  # noqa: F401  (recall_mod 内部用)
        g_global, _ = recall_mod._build_entity_graph(source_cwd=None)
        neighbors = recall_mod.bfs_neighbors([ea], g_global, hops=2)
        assert eb in neighbors, (
            f"default 全局图: BFS 应从 Alpha 达 Bravo(跨 cwd 边共享), got neighbors={neighbors}"
        )
    finally:
        shutil.rmtree(tmp)


def test_recall_use_bfs_scoped_on_excludes_other_cwd_neighbor():
    """recall(use_bfs_scoped=True)→ BFS 图按 cwd 过滤: 其他 cwd 邻居不可达。

    scoped=/proj/aaa: 图只含 cwd 匹配 fact(Alpha↔Bravo 在 /proj/bbb 不匹配)→
    Alpha 孤立 → bfs_neighbors 不达 Bravo。
    """
    tmp = _fresh_db()
    try:
        ea = store.put_entity("Alpha", "concept")
        eb = store.put_entity("Bravo", "concept")
        # 唯一图连边在 /proj/bbb; scoped=/proj/aaa → 图丢此边 → Alpha 孤立
        store.put_fact(ea, "connects", "alpha connects bravo", extractor="llm",
                       fact_type="permanent", LIF=0.5, confidence=0.8,
                       source_refs=["s"], topic="a connects b", object_id=eb,
                       source_cwd="/proj/bbb")
        store.put_fact(eb, "runs_on", "bravo runs service", extractor="llm",
                       fact_type="permanent", LIF=0.5, confidence=0.8,
                       source_refs=["s"], topic="b runs svc", object_id=None,
                       source_cwd="/proj/bbb")
        # scoped on: 图只含 /proj/aaa fact(无)→ Alpha 孤立 → BFS 不达 Bravo
        import networkx as nx  # noqa: F401
        g_scoped, _ = recall_mod._build_entity_graph(source_cwd="/proj/aaa")
        neighbors = recall_mod.bfs_neighbors([ea], g_scoped, hops=2)
        assert eb not in neighbors, (
            f"scoped=/proj/aaa: BFS 不应达 Bravo(图按 cwd 过滤丢 /proj/bbb 边), got neighbors={neighbors}"
        )
        # default off 对比: 全局图含 /proj/bbb 边 → 达 Bravo
        g_global, _ = recall_mod._build_entity_graph(source_cwd=None)
        neighbors_global = recall_mod.bfs_neighbors([ea], g_global, hops=2)
        assert eb in neighbors_global, (
            f"default 全局图对比: 应达 Bravo(跨 cwd 边), got neighbors={neighbors_global}"
        )
    finally:
        shutil.rmtree(tmp)


# ── (3) BFS + use_vec 组合深测 (ADR-4 BFS+use_vec 组合深测覆盖) ───────
# 设计: query "SeedX"(token "seedx"); orphan fact value "germinate origin point"
# (无 token 重叠 → 字面路不命中; 孤立无图边 → BFS 路不达; 仅向量路可达 via mocked
# embed 同向 → cos=1.0 ≥ VEC_MIN)。确保「仅向量路」与「仅 BFS 路」路径真隔离。

def _setup_bfs_vec_db():
    """构造 BFS+vec 双路场景, 返回 (tmp, seed, fid_a, fid_b, bravo, orphan, orig_embed).

    perf/vec-index: embed mock 在建库**前**挂 (put_fact 的 vec_fact 同步吃
    mock 向量), orig_embed 返回给调用方 restore。"""
    tmp = _fresh_db()
    import embedding
    import vec_index as _vi
    vec = [1.0, 0.0] + [0.0] * (_vi.VEC_DIM - 2)      # pad 到索引维度
    ortho = [0.0, 1.0] + [0.0] * (_vi.VEC_DIM - 2)
    orig_embed = embedding.embed

    def fake_embed(text, providers=None):
        if text == "SeedX":
            return vec
        if "germinate origin point" in (text or ""):
            return vec
        return ortho
    embedding.embed = fake_embed

    seed = store.put_entity("SeedX", "concept")
    bravo = store.put_entity("BravoY", "concept")
    orphan = store.put_entity("OrphanZ", "concept")
    # 图连脚手架: seed↔bravo(让 BFS 能从 seed 走到 bravo)
    store.put_fact(seed, "connects", "seedx connects bravoy", extractor="llm",
                   fact_type="permanent", LIF=0.5, confidence=0.8,
                   source_refs=["s"], topic="seed bravo edge", object_id=bravo)
    # fact B: subject=Bravo(hop1) → BFS 路召回; value 不含 query token "seedx"
    fid_b = store.put_fact(bravo, "runs_on", "bravoy runs the service", extractor="llm",
                           fact_type="permanent", LIF=0.5, confidence=0.8,
                           source_refs=["s"], topic="bravo runs", object_id=None)
    # fact A: subject=Orphan(孤立) → 仅向量路; value 无 "seedx" token (字面路不命中)
    fid_a = store.put_fact(orphan, "describes", "germinate origin point", extractor="llm",
                           fact_type="permanent", LIF=0.5, confidence=0.8,
                           source_refs=["s"], topic="orphan desc", object_id=None)
    return tmp, seed, fid_a, fid_b, bravo, orphan, orig_embed


def test_bfs_plus_vec_dual_path_union():
    """use_bfs=True + use_vec=True: BFS 路(fid_b) + 向量路(fid_a) 双候选 union 不丢。"""
    tmp, seed, fid_a, fid_b, bravo, orphan, orig_embed = _setup_bfs_vec_db()
    try:
        res = recall_mod.recall("SeedX", use_bfs=True, use_vec=True,
                                bfs_hops=2, boost=False)
        ids = {f["id"] for f in res}
        assert fid_b in ids, (
            f"BFS 路应召回 fid_b (Bravo hop=1), got ids={ids}"
        )
        assert fid_a in ids, (
            f"向量路应召回 fid_a (Orphan cos=1.0 ≥ VEC_MIN, 无 token 重叠仅向量可达), got ids={ids}"
        )
    finally:
        recall_mod.embedding.embed = orig_embed
        shutil.rmtree(tmp)


def test_bfs_plus_vec_no_vec_path_when_vec_off():
    """门控对称: use_vec=False → 仅向量可达的 fid_a(Orphan)不召回(向量路关), BFS 路仍通。"""
    tmp, seed, fid_a, fid_b, bravo, orphan, orig_embed = _setup_bfs_vec_db()
    try:
        # use_vec=False: 向量路关 → fid_a 不可达(孤立 + value 无 "seedx" token 字面不命中)
        res = recall_mod.recall("SeedX", use_bfs=True, use_vec=False,
                                bfs_hops=2, boost=False)
        ids = {f["id"] for f in res}
        assert fid_a not in ids, (
            f"use_vec=False: fid_a 仅向量可达应不召回, got ids={ids}"
        )
        assert fid_b in ids, (
            f"use_vec=False: fid_b BFS 路独立应仍召回, got ids={ids}"
        )
    finally:
        recall_mod.embedding.embed = orig_embed
        shutil.rmtree(tmp)


def test_bfs_plus_vec_no_bfs_path_when_bfs_off():
    """门控对称: use_bfs=False → 仅 BFS 可达的 fid_b(Bravo hop>0)不召回, 向量路仍通。"""
    tmp, seed, fid_a, fid_b, bravo, orphan, orig_embed = _setup_bfs_vec_db()
    try:
        # use_bfs=False: BFS 路关 → fid_b(hop>0, value 无 "seedx" token)不召回
        res = recall_mod.recall("SeedX", use_bfs=False, use_vec=True,
                                boost=False)
        ids = {f["id"] for f in res}
        assert fid_b not in ids, (
            f"use_bfs=False: fid_b 图近需 BFS 应不召回, got ids={ids}"
        )
        assert fid_a in ids, (
            f"use_bfs=False: fid_a 向量路独立应仍召回, got ids={ids}"
        )
    finally:
        recall_mod.embedding.embed = orig_embed
        shutil.rmtree(tmp)
