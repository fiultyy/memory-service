"""T1 集成测试: cli.ingest 真实接线 + resolver 实体合并 (ADR-D3).

monkeypatch adapter.extract_facts 产含同一实体两种写法的 edges → cli.ingest
经 resolver 合并 → 断言无孤儿 + fact 两端指向 resolved id + aliases 含异写。
db.init(tmp) 隔离, 前后断言 data/memory.db entity count 不变(零污染)。
"""
import sqlite3
import tempfile
from pathlib import Path

import adapter
import cli
import db
import embedding
import store
from llm_provider import EntityOut, EdgeOut, Extraction


# 生产 db entity count 基线(单独连接, 不走 db._conn, 零污染)。
_PROD_DB = Path(__file__).parent / "data" / "memory.db"


def _prod_entity_count() -> int:
    if not _PROD_DB.exists():
        return 0
    conn = sqlite3.connect(str(_PROD_DB))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='entity'"
        ).fetchone()
        if row is None:
            return 0
        return conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
    finally:
        conn.close()


# D8: 全程不开生产 embeddings.db (cache 指 tmp + clear_cache)。
embedding._CACHE_DB = Path(tempfile.mkdtemp()) / "embeddings.db"
embedding.clear_cache()

before = _prod_entity_count()

# 隔离: 全新 tmp db (绝不污染 data/memory.db)。
db.init(Path(tempfile.mkdtemp()) / "mem.db")

# monkeypatch adapter.extract_facts: 两条边都引用同一实体的两种写法
# ("A2A 原生 agent" / "a2a 原生 agent") → cli.ingest 经 resolver 应合并为同一 entity。
_ORIG_EXTRACT = adapter.extract_facts


def _fake_extract(text, providers=None, wings=None):
    return Extraction(
        entities=[
            EntityOut("A2A 原生 agent", "component", aliases=["a2a 原生 agent"]),
        ],
        edges=[
            # 边的 subject 用异写 "a2a 原生 agent" — cli phase2 resolve 它时,
            # resolver step1 应命中已建 "A2A 原生 agent"(alias gate)并并入异写。
            EdgeOut("a2a 原生 agent", "is_a", "A2A 原生 agent", topic="异写自指"),
        ],
        confidence=0.7, source_meta={"provider": "fake"})


adapter.extract_facts = _fake_extract
try:
    result = cli.ingest("A2A 原生 agent 与 a2a 原生 agent 是同一实体", source_ref="t1")
finally:
    adapter.extract_facts = _ORIG_EXTRACT

print(f"ingest result: {result}")

# 1. 无孤儿: 合并后库里只有 1 个 entity (两种写法 → 同一 resolved id)。
n_entities = store.count_entities()
assert n_entities == 1, (
    f"merge: expected 1 entity (two surface forms → same id), got {n_entities}")
resolved_ent = store.get_entity(
    db.get_conn().execute("SELECT id FROM entity").fetchone()["id"])
print(f"Test 1a (no orphan, merged): {resolved_ent['name']} aliases={resolved_ent['aliases']}")

# 2. 该 entity 的 aliases 含异写 (Step1 alias 对称: D7)。
assert "a2a 原生 agent" in [a.lower() for a in resolved_ent["aliases"]], (
    f"aliases should contain the surface form 'a2a 原生 agent', got {resolved_ent['aliases']}")
print(f"Test 1b (aliases contain alt spelling): {resolved_ent['aliases']}")

# 3. fact.subject_id 和 object_id 都指向 resolved id 且 IS NOT NULL (无孤儿 fact)。
conn = db.get_conn()
facts = conn.execute("SELECT subject_id, object_id FROM fact").fetchall()
assert len(facts) >= 1, f"expected at least 1 fact, got {len(facts)}"
for f in facts:
    assert f["subject_id"] is not None, f"subject_id must not be NULL, got {f['subject_id']}"
    assert f["object_id"] is not None, f"object_id must not be NULL, got {f['object_id']}"
    assert f["subject_id"] == resolved_ent["id"], (
        f"subject_id must point to resolved entity, got {f['subject_id']} vs {resolved_ent['id']}")
    assert f["object_id"] == resolved_ent["id"], (
        f"object_id must point to resolved entity, got {f['object_id']} vs {resolved_ent['id']}")
print(f"Test 1c (fact both ends → resolved id, non-NULL): {len(facts)} fact(s) OK")

# 4. 零污染: 生产 data/memory.db entity count 不变。
after = _prod_entity_count()
assert after == before, (
    f"prod data/memory.db must be untouched: before={before}, after={after}")
print(f"Test 1d (zero pollution): prod entity count before={before} after={after}")

print("\n✓ All integration tests passed")
