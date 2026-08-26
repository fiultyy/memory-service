"""resolver D3 两步实体合并自验证 (ADR-D3). db.init(tmp) 隔离, 绝不碰 data/memory.db.

不依赖 LLM/embedding 在线: providers=[] / embedding_providers=[] 隔离网络。
Step2 LLM 判定逻辑用 mock provider 验证(真实 LLM 判定留手动验)。
mock-vector 测试 monkeypatch embedding.embed(确定性, 不触 cache/网络)。
"""
import sqlite3
import tempfile
from pathlib import Path

import db
import embedding
import resolver
import store
from llm_provider import Extraction


def _fresh_db() -> None:
    """切换到一个全新 tmp db (隔离每个 test case, count 断言干净)。"""
    db.init(Path(tempfile.mkdtemp()) / "mem.db")


# ── D8: 全程不开生产 data/embeddings.db ──────────────────────────────
# 把 embedding L2 cache 指到 tmp + clear_cache 关掉老连接, 这样即便
# embedding_providers=[] 的离线路径走了 cache_lookup, 也只触 tmp, 不污染生产库。
embedding._CACHE_DB = Path(tempfile.mkdtemp()) / "embeddings.db"
embedding.clear_cache()

# ── Step 1 廉价闸: 大小写不敏感 name 精确命中 ──────────────────────
_fresh_db()
react_id = store.put_entity("React", "tool")
eid = resolver.resolve_entity("react", "tool", embedding_providers=[])
assert eid == react_id, f"case-insensitive name gate: expected {react_id}, got {eid}"
print(f"Test 1 (cheap gate, case-insensitive name): 'react' → {store.get_entity(eid)['name']}")

# ── Step 1 廉价闸: alias 精确命中 ──────────────────────────────────
_fresh_db()
nyc_id = store.put_entity("New York City", "concept", aliases=["NYC"])
eid = resolver.resolve_entity("NYC", "concept", embedding_providers=[])
assert eid == nyc_id, f"alias gate: expected {nyc_id}, got {eid}"
print(f"Test 2 (cheap gate, alias): 'NYC' → {store.get_entity(eid)['name']}")

# ── 降级(embedding 离线 + 无 LLM): 全新 name → 新建 entity(emb=[]) ──
_fresh_db()
eid = resolver.resolve_entity("BrandNewOfflineEntityX9", "concept",
                              providers=[], embedding_providers=[])
assert eid is not None, "offline + no LLM should still create an entity"
ent = store.get_entity(eid)
assert ent["name_embedding"] == [], (
    f"offline embedding must be [] (no network), got {ent['name_embedding']}")
assert store.count_entities() == 1, f"expected 1 entity, got {store.count_entities()}"
print(f"Test 3 (offline degrade): created '{ent['name']}' emb={ent['name_embedding']}")

# ── 下面 mock-vector 测试: monkeypatch embedding.embed 返回固定向量 ──
# (确定性, 不依赖 embeddings.db cache / 不触网络; resolver 在调用时查模块属性)
_orig_embed = embedding.embed
import vec_index as _vi
_VEC = [1.0, 0.0, 0.0] + [0.0] * (_vi.VEC_DIM - 3)  # perf/vec-index: pad 到索引维度 (小维 fixture 不入 vec0)
embedding.embed = lambda text, providers=None: list(_VEC)

# ── providers=[] 跳过 LLM: 有候选也不 merge, 直接新建 ──────────────
_fresh_db()
store.put_entity("ExistingVec", "tool", name_embedding=list(_VEC))  # 同向 → cosine 1.0
eid = resolver.resolve_entity("NewVecCandidate", "tool", providers=[])
existing = store.find_entity_exact("ExistingVec")
assert eid is not None and eid != existing["id"], (
    "providers=[] must NOT call LLM dedupe — should create a new entity")
assert store.count_entities() == 2, f"expected 2 (no merge), got {store.count_entities()}"
print("Test 4 (providers=[] skips LLM): 'NewVecCandidate' created, not merged")

# ── 核心 throwaway: alias 合 + 新建, count 准确 ────────────────────
_fresh_db()
nyc_id = store.put_entity("New York City", "concept", aliases=["NYC"])
eid_alias = resolver.resolve_entity("NYC", "concept", providers=[])
assert eid_alias == nyc_id, f"'NYC' should merge into New York City, got {eid_alias}"
eid_tokyo = resolver.resolve_entity("Tokyo", "concept", providers=[])
assert eid_tokyo is not None and eid_tokyo != nyc_id, "Tokyo should be a new entity"
assert store.count_entities() == 2, (
    f"expected exactly 2 entities (NYC merged, Tokyo new), got {store.count_entities()}")
print(f"Test 5 (throwaway): NYC→merge, Tokyo→new, count={store.count_entities()}")

# ── 同实体不同大小写: 两次 resolve → 1 entity ──────────────────────
_fresh_db()
foo_id = store.put_entity("Foo", "concept")
eid = resolver.resolve_entity("FOO", "concept", providers=[])
assert eid == foo_id, f"'FOO' should merge into Foo, got {eid}"
assert store.count_entities() == 1, (
    f"case-insensitive merge: expected 1 entity, got {store.count_entities()}")
print(f"Test 6 (case-insensitive merge): 'FOO' → Foo, count={store.count_entities()}")


# ── Step 2 LLM 合并(mock provider, 无网络) ────────────────────────
class _FakeLLM:
    base_url = None

    def extract_facts(self, text):
        return Extraction()

    def dedupe_entity(self, new_name, new_type, candidates):
        # mock: 总是并入 top 候选(模拟 LLM 判同义)
        return {"duplicate_id": candidates[0]["id"]} if candidates else {"duplicate_id": None}


_fresh_db()
js_id = store.put_entity("JavaScript", "tool", name_embedding=list(_VEC))
eid = resolver.resolve_entity("MockJSAliasZ1", "tool", providers=[_FakeLLM()])
assert eid == js_id, f"Step 2 LLM merge should return JavaScript id, got {eid}"
ent = store.get_entity(js_id)
assert "MockJSAliasZ1" in ent["aliases"], (
    f"merged name should land as alias, got {ent['aliases']}")
assert store.count_entities() == 1, (
    f"merge must not create a new entity, got {store.count_entities()}")
print(f"Test 7 (Step 2 mock merge): 'MockJSAliasZ1' → JavaScript, "
      f"aliases={ent['aliases']}, count={store.count_entities()}")

# ── Step 2 LLM 判"不合"(mock 返回 None) → 新建 ────────────────────
class _FakeLLMNoDup:
    base_url = None

    def extract_facts(self, text):
        return Extraction()

    def dedupe_entity(self, new_name, new_type, candidates):
        return {"duplicate_id": None}  # mock: 判不同义


_fresh_db()
store.put_entity("Python蟒蛇", "concept", name_embedding=list(_VEC))
eid = resolver.resolve_entity("PythonLangZ2", "tool", providers=[_FakeLLMNoDup()])
snake = store.find_entity_exact("Python蟒蛇")
assert eid is not None and eid != snake["id"], (
    "LLM says not-duplicate → must create new entity")
assert store.count_entities() == 2, f"expected 2 (no merge), got {store.count_entities()}"
print(f"Test 8 (Step 2 mock no-merge): 'PythonLangZ2' created, count={store.count_entities()}")

# ── T3(a): LLM 幻觉一个不在 candidates 的 phantom id → 不并入、走新建 ──
class _FakeLLMPhantom:
    base_url = None

    def extract_facts(self, text):
        return Extraction()

    def dedupe_entity(self, new_name, new_type, candidates):
        # mock: 抄 few-shot 示例 id, 不在真实 candidates 里(幻觉/phantom)
        return {"duplicate_id": "e_fewshot_phantom"}


_fresh_db()
store.put_entity("RealCandidate", "tool", name_embedding=list(_VEC))
eid = resolver.resolve_entity("PhantomProbeZ3", "tool", providers=[_FakeLLMPhantom()])
real = store.find_entity_exact("RealCandidate")
assert eid is not None and eid != real["id"], (
    "phantom id must be rejected — resolver should create a new entity, not crash")
assert store.count_entities() == 2, (
    f"phantom rejected → new entity, expected 2, got {store.count_entities()}")
print(f"Test 9 (phantom id rejected): 'PhantomProbeZ3' created (no crash), "
      f"count={store.count_entities()}")

# ── T3(b): 维度不匹配 → heal 语义 (perf/vec-index 迁移) ────────────
# 旧断言: dim-4 行被 _cosine_topk 跳过 → 新建孤儿。perf/vec-index 后语义
# 升级: 覆盖缺口触发 heal_entities_if_pending — 老结构行 re-embed(当前模型
# 维度) 落盘新结构并入索引 → 成正常候选 (D1 orphan 修复的维度不匹配推广;
# 孤儿新建正是老实现的 bug)。embed 被 mock 为 _VEC → heal 后同向 cosine 1.0
# → _FakeLLM 并入 (意图迁移: 维度异构不再造成孤儿)。
_fresh_db()
store.put_entity("DimMismatchEntity", "tool", name_embedding=[1.0, 0.0, 0.0, 0.0])  # dim=4
eid = resolver.resolve_entity("DimProbeZ4", "tool", providers=[_FakeLLM()])
mismatch = store.find_entity_exact("DimMismatchEntity")
assert eid == mismatch["id"], (
    f"dim-mismatch 行 heal 后应成候选并入 (不再孤儿新建), got {eid} vs {mismatch['id']}")
assert store.count_entities() == 1, (
    f"heal 语义: 1 entity (merged), got {store.count_entities()}")
# heal 落盘: 老裸 dim-4 行已升级当前维度新结构。
healed = store.get_entity(mismatch["id"])["name_embedding"]
assert len(healed) == len(_VEC), (
    f"heal 应 re-embed 落盘当前维度, got dim={len(healed)}")
print(f"Test 10 (dim-mismatch heal): 'DimProbeZ4' merged into healed candidate, "
      f"count={store.count_entities()}")

# ── D1 回填验证: step2 合并后, 候选 entity 的 emb 幂等不被覆盖 ─────
# (候选必已有同维 emb 才进 _cosine_topk; backfill 在此是 defensive no-op)
_fresh_db()
old_id = store.put_entity("OldEntityWithEmb", "tool", name_embedding=list(_VEC))
eid = resolver.resolve_entity("OldAliasZ5", "tool", providers=[_FakeLLM()])
assert eid == old_id, "should merge into OldEntityWithEmb (top cosine candidate)"
kept = store.get_entity(old_id)["name_embedding"]
assert kept == list(_VEC), (
    f"D1 step2 backfill: existing emb preserved (idempotent), got {kept}")
print(f"Test 11 (D1 step2 backfill idempotent): OldEntityWithEmb emb = {kept}")

# ── D1 回填 step1: 廉价闸命中后回填空 emb ─────────────────────────
_fresh_db()
hit_id = store.put_entity("HitEntityNoEmb", "tool")  # name_embedding=[]
resolver.resolve_entity("hitentitynoemb", "tool", providers=[])  # 大小写异写命中 step1
backfilled = store.get_entity(hit_id)["name_embedding"]
assert backfilled == list(_VEC), (
    f"D1 step1 backfill: cheap-gate hit should get emb, got {backfilled}")
print(f"Test 12 (D1 step1 backfill): HitEntityNoEmb emb backfilled = {backfilled}")

# ── D1 幂等: 已有 emb 的 entity 不被空/覆盖 ────────────────────────
_fresh_db()
keep_id = store.put_entity("KeepEmb", "tool", name_embedding=[0.5, 0.5, 0.0])
resolver.resolve_entity("keepemb", "tool", providers=[])  # step1 命中
kept = store.get_entity(keep_id)["name_embedding"]
assert kept == [0.5, 0.5, 0.0], (
    f"D1 idempotent: existing emb must not be overwritten, got {kept}")
print(f"Test 13 (D1 idempotent): existing emb preserved = {kept}")

# ── D1 orphan (skeptic must-fix): 离线 Phase1 插入的 emb=[] 实体, Phase2 用 ──
# vector provider re-resolve 同实体异写时, 必须能成为 cosine 候选并合并(count=1),
# 而非孤儿新建(count=2)。Test 11/12/13 只证明 merge 发生时 backfill 工作, 不证明
# emb=[] 实体能成为候选 — 这是 runner 逼出的真正 bug: step1 廉价闸(case-insensitive
# name)对 'JavaScript'/'JS' 不命中, _cosine_topk 又跳过 emb=[] 行 → 永不合并。
# 用一个真 EmbeddingProvider(embedding_providers=[] 时 embed() 返 [],=[P] 时返 vec)
# 精确模拟"Phase1 离线 / Phase2 在线"的真实接线, 而非 mock embed() 整体。
class _FakeEmbProvider:
    model = "fake"
    def __init__(self, v): self.v = v
    def embed(self, text): return list(self.v)


# 先还原 embed()(Test 4-13 用了 mock embed=lambda:_VEC), Phase1/2 走真 embed()
# 但传不同 embedding_providers — 这是与 mock-embed 关键差异: [] 真返 [], [P] 真返 vec。
embedding.embed = _orig_embed
_emb_provider = _FakeEmbProvider(_VEC)


# Phase1: embedding_providers=[] → emb=[] → put_entity 存 emb=[](离线 degrade)。
_fresh_db()
js_id1 = resolver.resolve_entity("JavaScript", "tool", providers=[],
                                 embedding_providers=[])
assert store.get_entity(js_id1)["name_embedding"] == [], (
    "Phase1 offline must insert emb=[] (embedding_providers=[] → embed returns [])")
assert store.count_entities() == 1, f"Phase1 count, got {store.count_entities()}"

# Phase2: 'JS' 同实体异写, step1 case-insensitive 不命中(≠ 'JavaScript'),
# 走 step2 — _cosine_topk 必须惰性 re-embed 'JavaScript'(emb=[] 行) 并回填,
# 让它成为候选, LLM 才能判同义合并。
js_id2 = resolver.resolve_entity("JS", "tool", providers=[_FakeLLM()],
                                 embedding_providers=[_emb_provider])
assert js_id2 == js_id1, (
    f"D1 orphan: 'JS' must merge into offline-inserted 'JavaScript' "
    f"(re-embed makes it a candidate), got js_id2={js_id2} != js_id1={js_id1}")
assert store.count_entities() == 1, (
    f"D1 orphan: must be 1 entity (merged), got {store.count_entities()}")
# 回填生效: 'JavaScript' 的 emb 现在非空(被 _cosine_topk re-embed 回填)。
backfilled = store.get_entity(js_id1)["name_embedding"]
assert backfilled == list(_VEC), (
    f"D1 orphan: 'JavaScript' emb must be backfilled by lazy re-embed, got {backfilled}")
print(f"Test 16 (D1 orphan fixed): 'JS' → offline 'JavaScript' (count=1, "
      f"emb backfilled={backfilled})")

# ── 空名 → None ────────────────────────────────────────────────────
_fresh_db()
assert resolver.resolve_entity("", "concept") is None, "empty name must return None"
print("Test 14 (empty name): resolve_entity('') → None")

# ── tmp 隔离确认 ───────────────────────────────────────────────────
assert str(db._conn_path).startswith("/tmp"), (
    f"conn must be on tmp, got {db._conn_path}")
print(f"Test 15 (tmp isolation): conn_path={db._conn_path}")

print("\nAll tests passed")
