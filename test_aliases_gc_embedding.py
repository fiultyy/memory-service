"""ADR-2②③ 测试: aliases GC + embedding 版本双认.

覆盖 (pytest 收集, def test_xxx() 非 module-level 裸 assert):
- set_aliases / remove_aliases (② store 原语)
- resolver GC: 合并 survivor 后清掉精确冗余别名(== survivor.name), 保留大小写异写
- _encode_embedding / _decode_embedding 双认(新结构 + 老裸 list + '[]' + NULL)
- backfill_entity_embedding 写新结构幂等(空行才填, 非空不覆盖)
- _cosine_topk 维度不匹配惰性 re-embed(老结构升级)

db.init(tmp) 隔离, 绝不碰 data/memory.db。embedding cache 指 tmp + clear_cache。
"""
import json
import tempfile
from pathlib import Path

import db
import embedding
import resolver
import store


def _fresh_db() -> None:
    """切换到全新 tmp db (隔离每个 test case)。"""
    db.init(Path(tempfile.mkdtemp()) / "mem.db")


# D8: 全程不开生产 embeddings.db — cache 指 tmp + clear_cache 关老连接。
embedding._CACHE_DB = Path(tempfile.mkdtemp()) / "embeddings.db"
embedding.clear_cache()


# ── set_aliases / remove_aliases (② store 原语) ──────────────────────

def test_set_aliases_full_replace_dedup():
    _fresh_db()
    eid = store.put_entity("X", "concept", aliases=["a", "b"])
    store.set_aliases(eid, ["c", "c", "d", ""])  # 去重 + 清空串
    ent = store.get_entity(eid)
    assert ent["aliases"] == ["c", "d"], f"set_aliases 全量替换去重清空, got {ent['aliases']}"


def test_set_aliases_none_to_empty():
    _fresh_db()
    eid = store.put_entity("X", "concept", aliases=["a"])
    store.set_aliases(eid, None)
    assert store.get_entity(eid)["aliases"] == [], "set_aliases(None) → []"


def test_set_aliases_missing_entity_noop():
    _fresh_db()
    store.set_aliases("nonexistent", ["a"])  # 不崩
    assert store.count_entities() == 0


def test_remove_aliases_partial():
    _fresh_db()
    eid = store.put_entity("X", "concept", aliases=["a", "b", "c"])
    store.remove_aliases(eid, ["b"])
    assert store.get_entity(eid)["aliases"] == ["a", "c"], "remove_aliases 移除指定"


def test_remove_aliases_missing_entity_noop():
    _fresh_db()
    store.remove_aliases("nonexistent", ["a"])  # 不崩


# ── resolver GC (②) ───────────────────────────────────────────────────

def test_gc_removes_exact_redundant_alias_keeps_case_variant():
    """合并 survivor 后: 别名精确==survivor.name 移除; 仅大小写不同保留(有效异写)。"""
    _fresh_db()
    # survivor "React", 手动塞入精确冗余别名 "React"(噪声) + 大小写异写 "react"(有效)。
    eid = store.put_entity("React", "tool")
    store.set_aliases(eid, ["React", "react", "REACT"])
    resolver._gc_aliases(eid, survivor_name="React")
    aliases = store.get_entity(eid)["aliases"]
    assert "React" not in aliases, "精确冗余(==name)必须移除"
    assert "react" in aliases and "REACT" in aliases, "大小写异写必须保留"
    assert aliases == ["react", "REACT"], f"保序去冗余, got {aliases}"


def test_resolve_step1_gc_no_self_alias_after_merge():
    """step1 case-insensitive 命中: surface form 若==survivor.name(精确)被 GC 移除,
    仅大小写异写保留。"""
    _fresh_db()
    store.put_entity("React", "tool")  # survivor
    # 'React' (精确同 survivor.name) → GC 移除; 但 surface 'React' 本就该并,
    # 验证别名里不残留精确 name, 且不崩。
    resolver.resolve_entity("React", "tool", embedding_providers=[])
    ent = store.find_entity_exact("React")
    aliases = store.get_entity(ent["id"])["aliases"]
    assert "React" not in aliases, f"survivor.name 不该残留为自己的别名, got {aliases}"
    assert store.count_entities() == 1


def test_resolve_step2_gc_alias_dedup():
    """step2 LLM 合并: surface form 记入别名后 GC 清掉精确冗余(case-variant 保留)。"""
    _fresh_db()
    import vec_index as _vi
    vec = [1.0, 0.0, 0.0] + [0.0] * (_vi.VEC_DIM - 3)  # perf/vec-index: pad
    js_id = store.put_entity("JavaScript", "tool", name_embedding=vec)

    class _FakeLLM:
        base_url = None
        def dedupe_entity(self, name, etype, candidates):
            return {"duplicate_id": candidates[0]["id"]} if candidates else {"duplicate_id": None}

    # monkeypatch embedding.embed 确定性返回 vec (不触 cache/网络)
    orig = embedding.embed
    embedding.embed = lambda text, providers=None: list(vec)
    try:
        resolver.resolve_entity("MockJSAlias", "tool", providers=[_FakeLLM()])
    finally:
        embedding.embed = orig
    ent = store.get_entity(js_id)
    assert "MockJSAlias" in ent["aliases"], "异写别名保留"
    assert "JavaScript" not in ent["aliases"], "精确 name 不残留"
    assert store.count_entities() == 1


# ── embedding 版本双认 (③) ────────────────────────────────────────────

def test_encode_embedding_new_structure():
    s = store._encode_embedding([0.1, 0.2, 0.3])
    doc = json.loads(s)
    assert doc["v"] == [0.1, 0.2, 0.3]
    assert doc["dim"] == 3
    assert "model" in doc


def test_encode_embedding_empty_is_empty_marker():
    assert store._encode_embedding([]) == "[]"
    assert store._encode_embedding(None) == "[]"


def test_decode_embedding_new_structure():
    raw = json.dumps({"v": [0.1, 0.2], "model": "m", "dim": 2})
    assert store._decode_embedding(raw) == [0.1, 0.2]


def test_decode_embedding_legacy_bare_list():
    """老库裸 list → 双认返回 list。"""
    raw = json.dumps([0.5, 0.5, 0.5])
    assert store._decode_embedding(raw) == [0.5, 0.5, 0.5]


def test_decode_embedding_null_and_empty_marker():
    assert store._decode_embedding(None) == []
    assert store._decode_embedding("[]") == []


def test_put_entity_writes_new_embedding_structure():
    _fresh_db()
    eid = store.put_entity("Tokyo", "concept", name_embedding=[0.1, 0.2, 0.3])
    # 读原始列(不经 _decode)验证落新结构
    conn = db.get_conn()
    raw = conn.execute("SELECT name_embedding FROM entity WHERE id=?", (eid,)).fetchone()[0]
    doc = json.loads(raw)
    assert isinstance(doc, dict), f"put_entity 必须写新结构 {{v,model,dim}}, got {raw}"
    assert doc["v"] == [0.1, 0.2, 0.3] and doc["dim"] == 3
    # get_entity (_decode 归一) 仍返 list
    assert store.get_entity(eid)["name_embedding"] == [0.1, 0.2, 0.3]


def test_decode_entity_reads_legacy_bare_list():
    """老库行(name_embedding=裸 list)经 get_entity 双认为 list[float]。"""
    _fresh_db()
    eid = store.put_entity("Legacy", "concept")
    conn = db.get_conn()
    conn.execute("UPDATE entity SET name_embedding=? WHERE id=?",
                 (json.dumps([0.4, 0.5, 0.6]), eid))  # 手塞老裸 list
    conn.commit()
    assert store.get_entity(eid)["name_embedding"] == [0.4, 0.5, 0.6], "老裸 list 双认"


def test_backfill_writes_new_structure_idempotent():
    _fresh_db()
    eid = store.put_entity("E", "concept")  # name_embedding=[] (空标记)
    n = store.backfill_entity_embedding(eid, [0.1, 0.2])
    assert n == 1, "空行回填 1 行"
    conn = db.get_conn()
    raw = conn.execute("SELECT name_embedding FROM entity WHERE id=?", (eid,)).fetchone()[0]
    doc = json.loads(raw)
    assert isinstance(doc, dict) and doc["v"] == [0.1, 0.2], f"backfill 写新结构, got {raw}"
    # 二次回填幂等(行已非空 → 0)
    assert store.backfill_entity_embedding(eid, [0.9, 0.9]) == 0, "非空行不覆盖(幂等)"


def test_backfill_fills_null_legacy_row():
    """老库 ALTER ADD name_embedding 无 DEFAULT → 迁移行 NULL, backfill 必须填。"""
    _fresh_db()
    eid = store.put_entity("E", "concept")
    conn = db.get_conn()
    conn.execute("UPDATE entity SET name_embedding=NULL WHERE id=?", (eid,))
    conn.commit()
    n = store.backfill_entity_embedding(eid, [0.1, 0.2, 0.3])
    assert n == 1, "NULL 行回填"
    assert store.get_entity(eid)["name_embedding"] == [0.1, 0.2, 0.3]


def test_backfill_skips_nonempty_legacy_bare_list():
    """老裸 list 行(非空)backfill 不覆盖(维度升级走 _cosine_topk 惰性 re-embed, 非 backfill)。"""
    _fresh_db()
    eid = store.put_entity("E", "concept")
    conn = db.get_conn()
    conn.execute("UPDATE entity SET name_embedding=? WHERE id=?",
                 (json.dumps([0.1, 0.2, 0.3, 0.4]), eid))  # 老裸 list dim=4
    conn.commit()
    n = store.backfill_entity_embedding(eid, [0.5, 0.5, 0.5])  # dim=3
    assert n == 0, "非空老裸 list 行 backfill 不覆盖"
    # 行不变(仍 dim=4 裸 list)
    raw = conn.execute("SELECT name_embedding FROM entity WHERE id=?", (eid,)).fetchone()[0]
    assert json.loads(raw) == [0.1, 0.2, 0.3, 0.4]


# ── _cosine_topk 维度不匹配惰性 re-embed (③) ─────────────────────────

def test_cosine_topk_lazy_reembed_dim_mismatch():
    """perf/vec-index 语义迁移: 老裸 list dim≠VEC_DIM 行 → heal_entities_if_pending
    re-embed(当前维度)落盘并入索引后成候选 (旧惰性 re-embed 语义由 heal 承接)。"""
    _fresh_db()
    import vec_index
    old_id = store.put_entity("OldDim4", "tool")
    conn = db.get_conn()
    conn.execute("UPDATE entity SET name_embedding=? WHERE id=?",
                 (json.dumps([1.0, 0.0, 0.0, 0.0]), old_id))
    conn.commit()
    # 触发覆盖缺口标记 (sync 走维度不匹配路径)。
    vec_now = [1.0, 0.0, 0.0] + [0.0] * (vec_index.VEC_DIM - 3)

    orig = embedding.embed
    orig_batch = embedding.embed_batch
    embedding.embed = lambda text, providers=None: list(vec_now)
    embedding.embed_batch = lambda texts, providers=None: [list(vec_now)] * len(texts)
    try:
        vec_index.sync_entity(old_id, [1.0, 0.0, 0.0, 0.0])  # dim 缺口 → pending
        healed = vec_index.heal_entities_if_pending()
        cands = resolver._cosine_topk(vec_now, 5, embedding_providers=[])
    finally:
        embedding.embed = orig
        embedding.embed_batch = orig_batch
    assert healed == 1, f"老结构行应被 heal, got {healed}"
    ids = [c["id"] for c in cands]
    assert old_id in ids, "heal 后老行应成 ANN 候选"
    cand = [c for c in cands if c["id"] == old_id][0]
    # vec0 cosine 距离 float32 精度 (~1e-4 漂移), 容差放宽。
    assert abs(cand["score"] - 1.0) < 1e-3


def test_cosine_topk_dim_mismatch_no_provider_skips():
    """维度不匹配 + 无 embedding_providers → 跳过(不崩, 不并入)。"""
    _fresh_db()
    old_id = store.put_entity("OldDim4", "tool")
    conn = db.get_conn()
    conn.execute("UPDATE entity SET name_embedding=? WHERE id=?",
                 (json.dumps([1.0, 0.0, 0.0, 0.0]), old_id))
    conn.commit()
    cands = resolver._cosine_topk([1.0, 0.0, 0.0], 5, embedding_providers=[])
    assert old_id not in [c["id"] for c in cands], "无 provider → dim 不匹配行跳过"


def test_cosine_topk_new_structure_matches():
    """新结构 {v,dim} 行 dim 匹配 → 正常入候选, 无需 re-embed。"""
    _fresh_db()
    import vec_index as _vi2
    v = [1.0, 0.0, 0.0] + [0.0] * (_vi2.VEC_DIM - 3)
    new_id = store.put_entity("NewStruct", "tool", name_embedding=v)
    cands = resolver._cosine_topk(v, 5, embedding_providers=[])
    assert new_id in [c["id"] for c in cands]


# ── B2: upsert 无条件覆盖 + _cosine_topk re-embed 落盘 ──────────────

def test_upsert_overwrites_legacy_bare_list():
    """upsert 无条件写新结构: 老裸 list 行(非空)也被覆盖(backfill 漏的 case)。"""
    _fresh_db()
    eid = store.put_entity("E", "concept")
    conn = db.get_conn()
    conn.execute("UPDATE entity SET name_embedding=? WHERE id=?",
                 (json.dumps([0.1, 0.2, 0.3, 0.4]), eid))  # 老裸 list dim=4
    conn.commit()
    # backfill 不覆盖(已知)
    assert store.backfill_entity_embedding(eid, [0.5, 0.5, 0.5]) == 0
    # upsert 无条件覆盖
    n = store.upsert_entity_embedding(eid, [0.5, 0.5, 0.5])
    assert n == 1, "upsert 应覆盖老裸 list 行(无条件 UPDATE)"
    raw = conn.execute("SELECT name_embedding FROM entity WHERE id=?", (eid,)).fetchone()[0]
    doc = json.loads(raw)
    assert "v" in doc and "model" in doc and "dim" in doc, f"应为新结构 {{v,model,dim}}, got raw={raw}"
    assert doc["v"] == [0.5, 0.5, 0.5], f"向量应覆盖为新值, got {doc['v']}"
    assert doc["dim"] == 3


def test_upsert_empty_vec_noop():
    """空向量 upsert 不写(不落盘空标记)。"""
    _fresh_db()
    eid = store.put_entity("E", "concept", name_embedding=[0.1, 0.2])
    assert store.upsert_entity_embedding(eid, []) == 0
    assert store.upsert_entity_embedding(eid, None) == 0
    # 原向量不变
    assert store.get_entity(eid)["name_embedding"] == [0.1, 0.2]


def test_cosine_topk_reembed_persists_to_db():
    """B2 root cause: _cosine_topk dim-mismatch re-embed 必须落盘(非仅内存)。

    老裸 list dim=4 行 → re-embed dim=3 → DB raw 应升级为新结构(非老裸 list)。
    之前调 backfill(WHERE 漏老裸list行)→ rowcount=0 不落盘 → 每次重算。
    """
    _fresh_db()
    import vec_index
    old_id = store.put_entity("OldDim4", "tool")
    conn = db.get_conn()
    conn.execute("UPDATE entity SET name_embedding=? WHERE id=?",
                 (json.dumps([1.0, 0.0, 0.0, 0.0]), old_id))  # 老裸 list dim=4
    conn.commit()
    vec_now = [1.0, 0.0, 0.0] + [0.0] * (vec_index.VEC_DIM - 3)

    orig = embedding.embed
    orig_batch = embedding.embed_batch
    embedding.embed = lambda text, providers=None: list(vec_now)
    embedding.embed_batch = lambda texts, providers=None: [list(vec_now)] * len(texts)
    try:
        vec_index.sync_entity(old_id, [1.0, 0.0, 0.0, 0.0])  # 缺口 → pending
        vec_index.heal_entities_if_pending()
    finally:
        embedding.embed = orig
        embedding.embed_batch = orig_batch

    # B2 核心断言 (语义迁移): heal 后 DB raw 应是新结构当前维度(非老裸 list)
    raw = conn.execute("SELECT name_embedding FROM entity WHERE id=?", (old_id,)).fetchone()[0]
    assert raw != json.dumps([1.0, 0.0, 0.0, 0.0]), (
        f"B2: heal 未落盘! raw 仍为老裸 list: {raw}"
    )
    doc = json.loads(raw)
    assert "v" in doc and doc["v"] == vec_now, (
        f"B2: heal 落盘应为新结构 v=当前维度, got raw={raw}"
    )
    # 二次调用不 re-embed(已落盘新结构 dim 匹配): 如果 _cosine_topk 仍调
    # embedding.embed → AssertionError 传播 → 测试 fail(不再静吞)。
    embedding.embed = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("不应 re-embed: 已落盘新结构 dim 匹配"))
    try:
        resolver._cosine_topk(vec_now, 5, embedding_providers=["fake"])
    finally:
        embedding.embed = orig
