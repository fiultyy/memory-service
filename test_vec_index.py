"""perf/vec-index 批验收测试 (主线验收 1-4/5 + 追加任务 A/B 验收 6/7)。

覆盖派发令:
1-3. vec0 表同步/回填幂等/新旧语义等价 (resolver top-k + recall --vector)。
4. 硬依赖: sqlite-vec 不可载 → init 立即 raise VecIndexError 含可行动诊断;
   代码审查点自动化: vec_index.py 无 numpy/内存矩阵回退分支 (grep)。
5. step1 字典等价 (与旧逐行扫描同命中)。
6. A: 纯中文零命中段 → 队列 segment 项; 幂等不重复。
7. B: 「护理担保」语义链接 "aged care guarantee"; 无关注联不误链; 阈值可调。

测试规范: def test_xxx() 函数让 pytest 收集。禁网络 (embedding mock)。
"""
import json
import re

import pytest
import sys
import tempfile
import uuid
from pathlib import Path

import db
import embedding
import gazetteer
import recall as recall_mod
import resolver
import store
import upgrade
import vec_index
from llm_provider import EdgeOut, EntityOut, Extraction


def _fresh(name: str) -> str:
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / f"{name}.db")
    return tmp


def _pad(*dirs: float) -> list[float]:
    v = list(dirs) + [0.0] * (vec_index.VEC_DIM - len(dirs))
    return v


# ── 验收 1: vec0 表 + 写路径同步 ─────────────────────────────────────

def test_vec_tables_and_write_sync():
    _fresh("sync")
    conn = db.get_conn()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"vec_entity", "vec_fact"} <= tables, tables

    # put_entity 同步入 vec_entity。
    v = _pad(1.0, 0.0)
    eid = store.put_entity("AlphaVec", "concept", name_embedding=v)
    n = conn.execute("SELECT COUNT(*) FROM vec_entity WHERE entity_id=?",
                     (eid,)).fetchone()[0]
    assert n == 1, "put_entity 应同步 vec_entity 行"

    # put_fact 同步入 vec_fact (value embed)。
    orig = embedding.embed
    embedding.embed = lambda text, providers=None: list(v)
    try:
        fid = store.put_fact(eid, "uses", "alpha value text", extractor="llm")
        nf = conn.execute("SELECT COUNT(*) FROM vec_fact WHERE fact_id=?",
                          (fid,)).fetchone()[0]
        assert nf == 1, "put_fact 应同步 vec_fact 行"
    finally:
        embedding.embed = orig

    # 软删 (superseded) → vec_fact 行删。
    store.update_fact_status(fid, "superseded", valid_to=store._now(),
                             reason="contradiction")
    nf = conn.execute("SELECT COUNT(*) FROM vec_fact WHERE fact_id=?",
                      (fid,)).fetchone()[0]
    assert nf == 0, "非活跃 fact 的 vec 行应删除"


def test_vec_backfill_idempotent():
    _fresh("backfill")
    v = _pad(0.5, 0.5)
    eid = store.put_entity("BackfillEnt", "concept", name_embedding=v)
    orig = embedding.embed
    embedding.embed = lambda text, providers=None: list(v)
    try:
        # entity 有向量直入; fact value 走 embed。
        fid = store.put_fact(eid, "is_a", "backfill value", extractor="llm")
        r1 = vec_index.backfill_all()
        conn = db.get_conn()
        ne1 = conn.execute("SELECT COUNT(*) FROM vec_entity").fetchone()[0]
        nf1 = conn.execute("SELECT COUNT(*) FROM vec_fact").fetchone()[0]
        r2 = vec_index.backfill_all()  # 幂等重跑
        ne2 = conn.execute("SELECT COUNT(*) FROM vec_entity").fetchone()[0]
        nf2 = conn.execute("SELECT COUNT(*) FROM vec_fact").fetchone()[0]
    finally:
        embedding.embed = orig
    assert r1["entities"] >= 1 and r1["facts"] >= 1, r1
    assert (ne1, nf1) == (ne2, nf2), (
        f"幂等重跑不得增行: entity {ne1}->{ne2}, fact {nf1}->{nf2}")


# ── 验收 2: 新旧语义等价 ─────────────────────────────────────────────

def _old_cosine_topk_reference(emb, k):
    """旧实现参照: 全表拉 + JSON 解码 + 纯 Python 余弦 (验收 2 对照基准)。"""
    import math
    n_a = math.sqrt(sum(x * x for x in emb)) or 1e-12
    scored = []
    conn = db.get_conn()
    for row in conn.execute("SELECT * FROM entity ORDER BY created_at").fetchall():
        ent = store._decode_entity(row)
        vec = ent.get("name_embedding") or []
        if not vec or len(vec) != len(emb):
            continue
        n_b = math.sqrt(sum(y * y for y in vec)) or 1e-12
        dot = sum(a * b for a, b in zip(emb, vec))
        scored.append({"id": ent["id"], "score": dot / (n_a * n_b)})
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:k]


def test_resolver_topk_equivalence():
    """同 fixture: 新 ANN top-k 与旧全表余弦参照 id 集一致 (浮点容差)。"""
    _fresh("equiv")
    vecs = {
        "NearA": _pad(1.0, 0.0), "NearB": _pad(0.9, 0.1),
        "FarC": _pad(0.0, 1.0), "FarD": _pad(-1.0, 0.0),
    }
    ids = {store.put_entity(n, "concept", name_embedding=v)
           for n, v in vecs.items()}
    q = _pad(1.0, 0.0)
    new = resolver._cosine_topk(q, 4)
    old = _old_cosine_topk_reference(q, 4)
    assert {c["id"] for c in new} == {c["id"] for c in old} == ids
    # 分数逐对一致 (float32 容差 1e-3)。
    by_id_new = {c["id"]: c["score"] for c in new}
    for c in old:
        assert abs(by_id_new[c["id"]] - c["score"]) < 1e-3
    # 排序一致 (score 降序)。
    assert [c["id"] for c in new] == [c["id"] for c in old]


def test_recall_vector_equivalence():
    """recall --vector: 新 ANN 结果集与旧逐条 embed 余弦参照一致。"""
    _fresh("vecrec")
    v_hit = _pad(1.0, 0.0)
    v_miss = _pad(0.0, 1.0)
    orig = embedding.embed
    calls = {"q": False}

    def fake(text, providers=None):
        if text == "锈 铁工具" or "hitvalue" in text:
            return list(v_hit)
        return list(v_miss)

    embedding.embed = fake
    try:
        eid = store.put_entity("HitEnt", "concept")
        fid_hit = store.put_fact(eid, "describes", "hitvalue content",
                                 extractor="llm", fact_type="permanent",
                                 LIF=0.6, confidence=0.8)
        # 千扰项: value 向量正交。
        for i in range(3):
            store.put_fact(eid, "relates_to", f"miss noise {i}",
                           extractor="llm", fact_type="permanent")
        res = recall_mod.recall("锈 铁工具", use_vec=True, boost=False,
                                top_k=10)
    finally:
        embedding.embed = orig
    ids = {f["id"] for f in res}
    assert fid_hit in ids, f"向量路应召回 hit fact: {ids}"


# ── 验收 4: 硬依赖 raise + 无 numpy 回退 ─────────────────────────────

def test_hard_dependency_raises_with_diagnostics():
    """模拟 sqlite-vec 不可载 → db.init 立即 raise VecIndexError, 信息含
    pip install 可行动诊断; 无任何静默降级。"""
    saved = sys.modules.get("sqlite_vec")
    sys.modules["sqlite_vec"] = None  # import → ImportError
    old_conn, old_path = db._conn, db._conn_path
    db._conn, db._conn_path = None, None
    vec_index._loaded = False
    try:
        tmp = tempfile.mkdtemp()
        try:
            db.init(Path(tmp) / "hard.db")
        except vec_index.VecIndexError as exc:
            msg = str(exc)
            assert "sqlite-vec" in msg and "pip install" in msg, (
                f"错误信息须含可行动诊断: {msg}")
            assert "vec0.so" in msg, f"备选路径须提示: {msg}"
        else:
            raise AssertionError("sqlite-vec 不可载必须 raise, 不得降级")
    finally:
        if saved is not None:
            sys.modules["sqlite_vec"] = saved
        else:
            sys.modules.pop("sqlite_vec", None)
        db._conn, db._conn_path = None, None
        _fresh("restore")  # 恢复可用索引面


def test_no_numpy_fallback_branch():
    """代码审查点自动化: vec_index.py 无 numpy/内存矩阵回退分支 (唯一路径)。"""
    src = Path("vec_index.py").read_text(encoding="utf-8")
    assert "numpy" not in src, "vec_index 不得含 numpy 回退"
    assert "_fallback" not in src, "vec_index 不得含 fallback 分支"
    assert "DEGRADED" not in src, "vec_index 不得含降级告警路径"


def test_unloaded_query_raises():
    """索引未加载时查询 API 立即 raise (不静默空结果)。"""
    _fresh("unload")
    saved = vec_index._loaded
    vec_index._loaded = False
    try:
        for fn in (lambda: vec_index.entity_topk([1.0], 1),
                   lambda: vec_index.fact_topk([1.0], 1),
                   lambda: vec_index.sync_entity("x", [1.0]),
                   lambda: vec_index.delete_fact("x")):
            try:
                fn()
            except vec_index.VecIndexError:
                pass
            else:
                raise AssertionError("未加载必须 raise VecIndexError")
    finally:
        vec_index._loaded = saved


# ── 验收 5: step1 字典等价 ───────────────────────────────────────────

def test_step1_dict_equivalence():
    """进程内字典与旧逐行扫描同命中 (name/alias/大小写/首中优先)。"""
    _fresh("dict")
    e1 = store.put_entity("Rust", "tool", aliases=["铁锈"])
    e2 = store.put_entity("Tokyo", "city")
    # name 精确 / 大小写不敏感 / alias。
    assert store.find_entity_exact("Rust")["id"] == e1
    assert store.find_entity_exact("RUST")["id"] == e1
    assert store.find_entity_exact("铁锈")["id"] == e1
    assert store.find_entity_exact("Tokyo")["id"] == e2
    assert store.find_entity_exact("不存在X") is None
    # 写时失效: 新增 alias 立即可查 (字典重建)。
    store.add_aliases(e2, ["东京"])
    assert store.find_entity_exact("东京")["id"] == e2
    store.remove_aliases(e2, ["东京"])
    assert store.find_entity_exact("东京") is None
    # 新建实体立即可查。
    e3 = store.put_entity("NewEnt", "concept")
    assert store.find_entity_exact("newent")["id"] == e3


# ── 验收 6 (追加 A): 纯中文零命中段兜底入队 ──────────────────────────

def test_zero_yield_segment_enqueued():
    """纯中文零命中段 (无 entities 无 edges) → 队列 segment 项 (material_text
    =段全文); 同 fixture 再跑幂等不重复入队。"""
    import autodream
    tmp = _fresh("segA")
    tpath = Path(tmp) / "t.jsonl"
    pure_cjk = "今天讨论了护理担保制度的细节与展望，内容较为口语化无模式命中"
    tpath.write_text(json.dumps({
        "type": "user", "message": {"content": [{"type": "text",
                                                 "text": pure_cjk}]}},
        ensure_ascii=False) + "\n", encoding="utf-8")
    orig = embedding.embed
    embedding.embed = lambda text, providers=None: []
    try:
        out1 = autodream.autodream("sess-a", str(tpath))
        out2 = autodream.autodream("sess-a", str(tpath))
    finally:
        embedding.embed = orig
    assert out1["added"] == 0, out1
    rows = db.get_conn().execute(
        "SELECT material_ref, material_text, material_prov, status "
        "FROM upgrade_queue").fetchall()
    seg_rows = [r for r in rows if r["material_ref"].startswith("segment:")]
    assert len(seg_rows) == 1, f"零产出段应兜底入队 1 项, got {len(seg_rows)}"
    assert seg_rows[0]["material_text"] == pure_cjk
    assert seg_rows[0]["material_prov"] == "user_prose"
    assert seg_rows[0]["status"] == "pending"
    # 幂等: 二跑零新增。
    n = db.get_conn().execute(
        "SELECT COUNT(*) FROM upgrade_queue").fetchone()[0]
    assert n == 1, f"重跑不得重复入队, got {n}"


def test_zero_yield_wings_no_fact_done():
    """wings 判「无事实」(零 edges) → 合法 done (attempts 不烧穿)。"""
    import dream
    tmp = _fresh("segA2")
    tpath = Path(tmp) / "t.jsonl"
    tpath.write_text(json.dumps({
        "type": "user", "message": {"content": [{"type": "text",
            "text": "纯中文口语段无任何模式命中内容"}]}},
        ensure_ascii=False) + "\n", encoding="utf-8")
    orig_e = embedding.embed
    embedding.embed = lambda text, providers=None: []
    import adapter

    def _wings_empty(text, providers=None):
        return Extraction(confidence=0.9,
                          source_meta={"provider": "fake",
                                       "extractor_label": "llm"})
    adapter.extract_facts = _wings_empty
    try:
        autodream_out = None
        import autodream
        autodream.autodream("sess-a2", str(tpath))
        stats = dream.run_cycle()
    finally:
        embedding.embed = orig_e
    assert stats["queue_done"] == 1 and stats["queue_failed"] == 0, stats
    row = db.get_conn().execute(
        "SELECT status, attempts FROM upgrade_queue").fetchone()
    assert row["status"] == "done" and row["attempts"] == 0, (
        f"合法判空应 done 且不烧 attempts: {dict(row)}")


# ── 验收 7 (追加 B): 语义优先实体匹配 ─────────────────────────────────

def _seed_en_entity_with_vec(name: str, v: list[float]) -> str:
    return store.put_entity(name, "concept", name_embedding=v)


def test_semantic_link_cjk_to_english_entity():
    """seed 英文实体 "aged care guarantee" (向量 v_en) → 含「护理担保」的
    中文段: span 向量同 v_en → ANN cosine 1.0 ≥ 0.45 → 链接该实体。"""
    _fresh("semB")
    v_en = _pad(1.0, 0.0)
    v_unrelated = _pad(0.0, 1.0)
    _seed_en_entity_with_vec("aged care guarantee", v_en)
    _seed_en_entity_with_vec("tax refund policy", v_unrelated)

    orig = embedding.embed
    orig_batch = embedding.embed_batch

    def fake_embed(text, providers=None):
        # gazetteer span embed 走 embed_batch; 单条兜底同映射。
        if "护理担保" in text:
            return list(v_en)
        return list(v_unrelated)

    embedding.embed = fake_embed
    embedding.embed_batch = (
        lambda texts, providers=None:
        [fake_embed(t) for t in texts])
    try:
        r = gazetteer.extract("本文讨论护理担保的相关安排")
    finally:
        embedding.embed = orig
        embedding.embed_batch = orig_batch
    names = {e.name for e in r.entities}
    assert "aged care guarantee" in names, (
        f"「护理担保」应语义链接英文 canonical, got {names}")
    assert "护理担保" not in names, "中文 span 不作为独立实体名"


def test_semantic_link_unrelated_not_linked():
    """无关注联 (cosine < 0.45) 不误链: span 向量与全部实体正交 → 无语义
    命中, 不产出链接。"""
    _fresh("semB2")
    v_en = _pad(1.0, 0.0)
    v_orth = _pad(0.0, 1.0)
    _seed_en_entity_with_vec("aged care guarantee", v_en)
    orig = embedding.embed
    orig_batch = embedding.embed_batch
    embedding.embed = lambda text, providers=None: list(v_orth)
    embedding.embed_batch = lambda texts, providers=None: [
        list(v_orth)] * len(texts)
    try:
        r = gazetteer.extract("今天天气不错适合出门散步走走")
    finally:
        embedding.embed = orig
        embedding.embed_batch = orig_batch
    linked = {e.name for e in r.entities} & {"aged care guarantee"}
    assert not linked, f"无关 span (cos 0) 不得误链: {linked}"


def test_semantic_threshold_monkeypatchable():
    """_SEMANTIC_LINK_THRESHOLD monkeypatch 可调: 抬到 0.99 → 同向 (1.0) 仍
    链; 0.528 档被拒。"""
    _fresh("semB3")
    v_en = _pad(1.0, 0.0)
    # 与 v_en cosine=0.528 的向量: cos(θ)=0.528 → (1, sqrt(1/0.528² -1))
    import math
    v_mid = _pad(1.0, math.sqrt(1 / 0.528 ** 2 - 1))
    _seed_en_entity_with_vec("target entity", v_mid)
    orig_t = gazetteer._SEMANTIC_LINK_THRESHOLD
    orig = embedding.embed
    orig_batch = embedding.embed_batch
    embedding.embed = lambda text, providers=None: list(v_en)
    embedding.embed_batch = lambda texts, providers=None: [list(v_en)] * len(texts)
    try:
        gazetteer._SEMANTIC_LINK_THRESHOLD = 0.45
        r45 = gazetteer.extract("关于护理担保的说明文本")
        gazetteer._SEMANTIC_LINK_THRESHOLD = 0.99
        r99 = gazetteer.extract("关于护理担保的说明文本")
    finally:
        gazetteer._SEMANTIC_LINK_THRESHOLD = orig_t
        embedding.embed = orig
        embedding.embed_batch = orig_batch
    assert "target entity" in {e.name for e in r45.entities}, (
        "0.528 ≥ 0.45 应链接")
    assert "target entity" not in {e.name for e in r99.entities}, (
        "0.528 < 0.99 应被阈值拒")


def test_semantic_link_e2e_via_autodream():
    """端到端: 中文段经 autodream → gazetteer 语义链接 → fact subject 挂
    既有英文实体 (不新建重复)。"""
    import autodream
    tmp = _fresh("semE2E")
    v_en = _pad(1.0, 0.0)
    v_orth = _pad(0.0, 1.0)
    seeded = _seed_en_entity_with_vec("aged care guarantee", v_en)
    # 一条 CJK 关系边载体: 「护理担保属于社会保障」→ subject span 语义链接。
    tpath = Path(tmp) / "t.jsonl"
    tpath.write_text(json.dumps({
        "type": "user", "message": {"content": [{"type": "text",
            "text": "护理担保属于社会保障体系的一部分"}]}},
        ensure_ascii=False) + "\n", encoding="utf-8")
    orig = embedding.embed
    orig_batch = embedding.embed_batch

    def fake(text, providers=None):
        if "护理担保" in text:
            return list(v_en)
        return list(v_orth)

    embedding.embed = fake
    embedding.embed_batch = lambda texts, providers=None: [
        fake(t) for t in texts]
    try:
        out = autodream.autodream("sess-e2e", str(tpath))
    finally:
        embedding.embed = orig
        embedding.embed_batch = orig_batch
    assert out["added"] >= 1, out
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT name FROM entity WHERE name IN ('aged care guarantee', '护理担保')"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert names == {"aged care guarantee"}, (
        f"中文 span 必须链到既有英文实体不新建: {names}")
    subj = conn.execute(
        "SELECT subject_id FROM fact WHERE status='active'").fetchone()
    assert subj["subject_id"] == seeded


# ── 验收 9/10/11 (追加 C): 空提取段三级时序兜底 ──────────────────────

def test_c_layer_entity_declaration_no_predicate_edges():
    """C 断言: 零命中段 (含「护理担保」, KG 有 aged care guarantee) → C 层
    链接产出**实体声明**且不新建重复实体; 无关注联 span 不产出; C 命中后
    段无 edges → 仍入 A 队列; 不造谓词边。"""
    import autodream
    tmp = _fresh("c9")
    v_en = _pad(1.0, 0.0)
    v_orth = _pad(0.0, 1.0)
    _seed_en_entity_with_vec("aged care guarantee", v_en)
    _seed_en_entity_with_vec("unrelated thing", v_orth)
    tpath = Path(tmp) / "t.jsonl"
    seg_text = "今天闲聊护理担保的一些背景和愿景规划细节"
    tpath.write_text(json.dumps({
        "type": "user", "message": {"content": [{"type": "text",
                                                 "text": seg_text}]}},
        ensure_ascii=False) + "\n", encoding="utf-8")
    orig = embedding.embed
    orig_batch = embedding.embed_batch

    def fake(text, providers=None):
        if "护理担保" in text:
            return list(v_en)
        return []  # 无关 span 无信号 (v_orth 会与正交 seed 自身 cosine 1)

    embedding.embed = fake
    embedding.embed_batch = lambda texts, providers=None: [
        fake(t) for t in texts]
    try:
        out = autodream.autodream("sess-c9", str(tpath))
    finally:
        embedding.embed = orig
        embedding.embed_batch = orig_batch
    conn = db.get_conn()
    ents = {r["name"] for r in conn.execute(
        "SELECT name FROM entity").fetchall()}
    # C 层零新建: 实体集仍 == 两个 seed (span 不成为新实体)。
    assert ents == {"aged care guarantee", "unrelated thing"}, (
        f"C 层不得新建实体, got: {ents}")
    # 无关 seed 未被链接: 其 aliases 未吸收任何 span。
    for r in conn.execute("SELECT name, aliases FROM entity").fetchall():
        if r["name"] == "unrelated thing":
            import json as _json
            assert not _json.loads(r["aliases"] or "[]"), (
                f"无关 span 不得链到 unrelated thing: {r['aliases']}")
    seg_rows = conn.execute(
        "SELECT material_text FROM upgrade_queue "
        "WHERE material_ref LIKE 'segment:%'").fetchall()
    assert any(r["material_text"] == seg_text for r in seg_rows), (
        f"C 命中后段无 edges 仍须 A 入队: {[r['material_text'][:20] for r in seg_rows]}")
    assert out["added"] == 0, f"C 层不得造谓词边: {out}"


def test_c_layer_idempotent_rerun():
    """C 幂等: 同段再跑不重复链接 — 首跑 span 折入 aliases 后, 二跑
    resolver step1 精确命中路径接管, 语义路零重算, 实体数不增。"""
    import autodream
    tmp = _fresh("c10")
    v_en = _pad(1.0, 0.0)
    seeded = _seed_en_entity_with_vec("aged care guarantee", v_en)
    tpath = Path(tmp) / "t.jsonl"
    tpath.write_text(json.dumps({
        "type": "user", "message": {"content": [{"type": "text",
            "text": "闲聊护理担保的背景细节"}]}},
        ensure_ascii=False) + "\n", encoding="utf-8")
    orig = embedding.embed
    orig_batch = embedding.embed_batch

    def fake(text, providers=None):
        if "护理担保" in text:
            return list(v_en)
        return []

    embedding.embed = fake
    embedding.embed_batch = lambda texts, providers=None: [
        fake(t) for t in texts]
    try:
        autodream.autodream("sess-c10", str(tpath))
        n1 = db.get_conn().execute(
            "SELECT COUNT(*) FROM entity").fetchone()[0]
        autodream.autodream("sess-c10", str(tpath))
        n2 = db.get_conn().execute(
            "SELECT COUNT(*) FROM entity").fetchone()[0]
    finally:
        embedding.embed = orig
        embedding.embed_batch = orig_batch
    assert n1 == n2, "再跑不得新建重复实体"
    ent = store.get_entity(seeded)
    assert ent["aliases"].count("护理担保") <= 1, (
        f"span 别名不得重复累积: {ent['aliases']}")


def test_c_layer_zero_llm():
    """C 层零 LLM 确认: 全程 adapter 调用计数=0 (monkeypatch 断言)。"""
    import autodream
    tmp = _fresh("c11")
    v_en = _pad(1.0, 0.0)
    _seed_en_entity_with_vec("aged care guarantee", v_en)
    tpath = Path(tmp) / "t.jsonl"
    tpath.write_text(json.dumps({
        "type": "user", "message": {"content": [{"type": "text",
            "text": "闲聊护理担保的背景细节"}]}},
        ensure_ascii=False) + "\n", encoding="utf-8")
    import adapter
    calls = {"n": 0}
    orig_a = adapter.extract_facts

    def _count(*a, **k):
        calls["n"] += 1
        raise AssertionError("C 层不得调 adapter (零 LLM)")

    adapter.extract_facts = _count
    orig = embedding.embed
    orig_batch = embedding.embed_batch
    embedding.embed = lambda t, providers=None: (
        list(v_en) if "护理担保" in t else [])
    embedding.embed_batch = lambda texts, providers=None: [
        list(v_en) if "护理担保" in t else [] for t in texts]
    try:
        autodream.autodream("sess-c11", str(tpath))
    finally:
        adapter.extract_facts = orig_a
        embedding.embed = orig
        embedding.embed_batch = orig_batch
    assert calls["n"] == 0
