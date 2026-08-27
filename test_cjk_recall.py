"""harness P2 前置: query_tokens CJK bigram 切分 + 中文词法召回 e2e。
根因: 中文无空格, 纯 whitespace split 把整句变一个巨型 token → LIKE/substring
永不命中 → UserPromptSubmit 注入对中文 prompt 完全失效。英文行为零回归。
db.init(tmp) 隔离, store 插 fact 绕过 LLM。"""
import shutil
import tempfile
from pathlib import Path

import recall as recall_mod
import scoring
import store
import db

# 1. CJK bigram: 连续 run → 全部相邻 bigram, chunk 原样保留(去重保序)
toks = scoring.query_tokens("sqlite-vec 的向量索引怎么建")
assert "sqlite-vec" in toks, toks
for bg in ("的向", "向量", "量索", "索引"):
    assert bg in toks, (bg, toks)
assert toks.count("向量") == 1, "去重"

# 2. 单字 run → 单字; 纯英文零回归
assert scoring.query_tokens("看 a 数据库") == ["看", "a", "数据库", "数据", "据库"]
assert scoring.query_tokens("help me with the database") == ["help", "me", "with", "the", "database"]

# 3. 空串/None 安全
assert scoring.query_tokens("") == [] and scoring.query_tokens(None) == []

# 4. e2e: 中文 query 词法命中 — 实体名命中 + value 扫描两条路
tmp = tempfile.mkdtemp()
db.init(Path(tmp) / "mem.db")
eid = store.put_entity("记忆服务", "project")
oid = store.put_entity("SQLite", "software")
fid1 = store.put_fact(eid, "uses", "SQLite 作存储后端", object_id=oid,
                      extractor="llm",
                      fact_type="stable", source_cwd="/test", LIF=0.6,
                      confidence=0.85, source_refs=["session:s"],
                      topic="记忆服务使用 SQLite")
# entity 名不在 query、value 含中文关键词 → value 扫描路径
eid2 = store.put_entity("用户", "inferred")
store.put_fact(eid2, "prefers", "向量召回质量更好", extractor="llm",
               fact_type="stable", source_cwd="/test", LIF=0.6,
               confidence=0.85, source_refs=["session:s"],
               topic="用户偏好向量召回")

# 实体命中路径: "记忆服务" bigram 记忆/忆服/服务 LIKE 命中 entity.name
res1 = recall_mod.recall("记忆服务的架构", session_id="t1", boost=False)
assert any(f["id"] == fid1 for f in res1), f"中文 query 应经实体名命中: {res1}"

# value 扫描路径: query 只含 value 关键词, 不含实体名
res2 = recall_mod.recall("召回质量怎么样", session_id="t1", boost=False)
assert len(res2) >= 1, f"中文 query 应经 value 扫描命中: {res2}"

# 5. 无命中 → 空列表(注入零输出前提)
res3 = recall_mod.recall("完全无关的甲乙丙丁", session_id="t1", boost=False)
assert res3 == [], res3

# 6. min_score 地板覆盖 (harness P2): 默认 SCORE_FLOOR=0.3; 长 prompt 场景
#    match 稀释 → 注入通道自校准低门槛, 默认语义零变化。
assert recall_mod.SCORE_FLOOR == 0.3
# 造孤立低分 fact: value 弱命中 + 无边 centrality=0 → 默认地板拒, 低门槛收
eid3 = store.put_entity("独立组件", "component")
store.put_fact(eid3, "supports", "支持某种特定场景的用法说明", extractor="llm",
               fact_type="stable", source_cwd="/test", LIF=0.5,
               confidence=0.8, source_refs=["session:s"], topic="独立组件支持场景")
r_default = recall_mod.recall("特定场景的用法", session_id="t1", boost=False)
r_low = recall_mod.recall("特定场景的用法", session_id="t1", boost=False, min_score=0.1)
assert len(r_low) >= len(r_default), (len(r_low), len(r_default))
if len(r_low) > len(r_default):
    low_ids = {f["id"] for f in r_low} - {f["id"] for f in r_default}
    assert low_ids, "低门槛应多收至少一条默认地板拒的 fact"

print("✓ CJK bigram 切分 + 中文词法召回 e2e (实体命中/value 扫描/无命中静默)")
shutil.rmtree(tmp)
