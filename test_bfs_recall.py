"""D5 BFS 召回 + D6 门控测试。
db.init(tmp) 隔离, store 插 fact 绕过 LLM。
图结构: A --uses--> B --runs_on--> C --depends_on--> D
  A-B: 1-hop, B-C: 1-hop, C-D: 1-hop
  A 到 C: 2-hop, A 到 D: 3-hop
"""
import shutil
import tempfile
from pathlib import Path

import db
import recall as recall_mod
import scoring
import store

tmp = tempfile.mkdtemp()
db.init(Path(tmp) / "mem.db")

# ── 构图: A→B→C→D 链 ──
ea = store.put_entity("Alpha", "concept")
eb = store.put_entity("Bravo", "concept")
ec = store.put_entity("Charlie", "concept")
ed = store.put_entity("Delta", "concept")

fid_ab = store.put_fact(ea, "uses", "Alpha uses Bravo", extractor="llm",
                        fact_type="permanent", LIF=0.5, confidence=0.8,
                        source_refs=["s"], topic="A uses B", object_id=eb)
fid_bc = store.put_fact(eb, "runs_on", "Bravo runs on Charlie", extractor="llm",
                        fact_type="permanent", LIF=0.5, confidence=0.8,
                        source_refs=["s"], topic="B runs on C", object_id=ec)
# fid_cd: subject=Charlie(2-hop from A), object=Delta(3-hop from A)
# Only reachable through Charlie — proper hop-cap test target
fid_cd = store.put_fact(ec, "depends_on", "Charlie depends on Delta", extractor="llm",
                        fact_type="permanent", LIF=0.5, confidence=0.8,
                        source_refs=["s"], topic="C depends on D", object_id=ed)

# ── 1. BFS 能召回 2-hop fact (use_bfs=True, hops=2) ──
# query="Alpha" → seed=Alpha → BFS 2-hop → Bravo(1), Charlie(2)
# fid_bc 是 Bravo(1-hop)的 fact → 召回; Charlie 也在 2-hop 内
res_bfs = recall_mod.recall("Alpha", use_bfs=True, bfs_hops=2, boost=False)
fact_ids_bfs = {f["id"] for f in res_bfs}
assert fid_bc in fact_ids_bfs, (
    f"BFS 应召回 fid_bc (通过 Bravo 1-hop / Charlie 2-hop), got ids={fact_ids_bfs}"
)
print(f"✓ BFS on (hops=2): fid_bc recalled, ids={fact_ids_bfs}")

# ── 2. 门控: use_bfs=False (default) → fid_bc 不在结果 ──
# query="Alpha" 字面/向量都不命中 Bravo/Charlie
res_nobfs = recall_mod.recall("Alpha", boost=False)
fact_ids_nobfs = {f["id"] for f in res_nobfs}
assert fid_bc not in fact_ids_nobfs, (
    f"BFS off: fid_bc 不应被召回(门控), got ids={fact_ids_nobfs}"
)
assert fid_cd not in fact_ids_nobfs, (
    f"BFS off: fid_cd 不应被召回(门控), got ids={fact_ids_nobfs}"
)
print(f"✓ BFS off: neither fid_bc nor fid_cd recalled (gating), ids={fact_ids_nobfs}")

# ── 3. hop cap: bfs_hops=1 → fid_cd 不召回(Charlie 2-hop, 不达) ──
# hops=1: BFS from Alpha reaches Alpha(0), Bravo(1)
# fid_bc IS recalled (Bravo is subject, hop 1)
# fid_cd NOT recalled (Charlie is at hop 2, not in BFS result at hops=1)
res_h1 = recall_mod.recall("Alpha", use_bfs=True, bfs_hops=1, boost=False)
fact_ids_h1 = {f["id"] for f in res_h1}
assert fid_cd not in fact_ids_h1, (
    f"hops=1: fid_cd 不应被召回(Charlie 在 2-hop 外), got ids={fact_ids_h1}"
)
assert fid_ab in fact_ids_h1, f"hops=1: fid_ab 应在结果(直接 match)"
print(f"✓ hops=1 cap: fid_cd not recalled (Charlie at 2-hop), ids={fact_ids_h1}")

# ── 3b. hop cap: bfs_hops=2 → fid_cd 召回(Charlie 2-hop, 达) ──
res_h2 = recall_mod.recall("Alpha", use_bfs=True, bfs_hops=2, boost=False)
fact_ids_h2 = {f["id"] for f in res_h2}
assert fid_cd in fact_ids_h2, (
    f"hops=2: fid_cd 应被召回(Charlie 在 2-hop 内), got ids={fact_ids_h2}"
)
print(f"✓ hops=2: fid_cd recalled (Charlie at 2-hop), ids={fact_ids_h2}")

# ── 4. max_nodes cap: bfs_neighbors 返回 ≤ max_nodes ──
db.init(Path(tmp) / "star.db")
star_center = store.put_entity("StarCenter", "concept")
for i in range(60):
    leaf = store.put_entity(f"Leaf{i}", "concept")
    store.put_fact(star_center, "connects", f"StarCenter connects Leaf{i}",
                   extractor="llm", fact_type="permanent", LIF=0.5,
                   confidence=0.8, source_refs=["s"], topic=f"star leaf {i}",
                   object_id=leaf)

import networkx as nx
g, _ = recall_mod._build_entity_graph()
bfs_res = recall_mod.bfs_neighbors([star_center], g, hops=2, max_nodes=50)
assert len(bfs_res) <= 50, (
    f"max_nodes=50: bfs_neighbors 返回 {len(bfs_res)} > 50"
)
assert star_center in bfs_res, "seed entity 应在结果(hop=0)"
print(f"✓ max_nodes cap: bfs_neighbors returned {len(bfs_res)} ≤ 50")

# ── 5. hop_decay 正确性 ──
assert recall_mod._hop_decay(0) == 1.0
assert recall_mod._hop_decay(1) == 0.5
assert recall_mod._hop_decay(2) == 0.25
assert recall_mod._hop_decay(-1) == 0.0
print("✓ hop_decay: 0→1.0, 1→0.5, 2→0.25, -1→0.0")

# ── 6. bfs_neighbors 空输入 → {} ──
assert recall_mod.bfs_neighbors([], g, hops=2) == {}, "空 seed → {}"
empty_g = nx.Graph()
assert recall_mod.bfs_neighbors(["x"], empty_g, hops=2) == {}, "空图 → {}"
print("✓ bfs_neighbors: empty seed/graph → {}")

# ── 7. 零回归: use_bfs=False 时 score_fact 不含 bfs_proximity 贡献 ──
s = scoring.score_fact({"value": "test", "LIF": 0.5, "confidence": 0.8}, "test")
assert s["bfs_proximity"] == 0.0, "default bfs_proximity 应为 0.0"
s2 = scoring.score_fact({"value": "test", "LIF": 0.5, "confidence": 0.8}, "test",
                         bfs_proximity=0.5)
assert s2["score"] >= s["score"], "bfs_proximity > 0 应增加 score"
assert s2["bfs_proximity"] == 0.5
print(f"✓ zero-regression: default bfs_proximity=0.0, score with bfs={s2['score']:.4f} >= without={s['score']:.4f}")

# ── 8. db 隔离: data/memory.db entity count 不变 ──
production_db = Path("/home/yy/projects/memory-service/data/memory.db")
if production_db.exists():
    import sqlite3
    before = sqlite3.connect(str(production_db)).execute("SELECT count(*) FROM entity").fetchone()[0]
    recall_mod.recall("Alpha", use_bfs=True, boost=False)
    after = sqlite3.connect(str(production_db)).execute("SELECT count(*) FROM entity").fetchone()[0]
    assert before == after, f"production db entity count changed: {before} → {after}"
    print(f"✓ db isolation: production entity count stable ({before})")

# ── 9. as_of + BFS 组合深测 (ADR-3/4): _build_entity_graph + bfs_neighbors 都透 as_of ──
# 场景: fid_ab 在 t1 valid, t2 superseded(valid_to=t2)。
#   as_of=t1.5 (区间内) → 图含 A↔B 边 → BFS 达 B → fid_ab/fid_bc 召回。
#   as_of=t3  (已失效)  → 图丢 A↔B 边 → BFS 不达 B → fid_ab/fid_bc 不召回。
# 双路径断言:
#   (1) _build_entity_graph(as_of) — 边按时间过滤(决定 BFS 能否走到 B);
#   (2) _facts_for_entities(as_of) — fact 按 valid_from/valid_to 过滤(决定召回集)。
t1 = "2026-07-01T00:00:00+00:00"
t1_5 = "2026-07-15T00:00:00+00:00"
t2 = "2026-08-01T00:00:00+00:00"
t3 = "2026-09-01T00:00:00+00:00"

db.init(Path(tmp) / "asof_bfs.db")
ea2 = store.put_entity("Alpha2", "concept")
eb2 = store.put_entity("Bravo2", "concept")
ec2 = store.put_entity("Charlie2", "concept")
# fid_ab2: valid_from=t1, 后续手动 set valid_to=t2 (区间内有效,t3 后失效)
fid_ab2 = store.put_fact(ea2, "uses", "Alpha2 uses Bravo2", valid_from=t1,
                         extractor="llm", fact_type="permanent", LIF=0.5,
                         confidence=0.8, source_refs=["s"], topic="A2 uses B2",
                         object_id=eb2)
fid_bc2 = store.put_fact(eb2, "runs_on", "Bravo2 runs on Charlie2", valid_from=t1,
                         extractor="llm", fact_type="permanent", LIF=0.5,
                         confidence=0.8, source_refs=["s"], topic="B2 runs on C2",
                         object_id=ec2)
conn_test = db.get_conn()
conn_test.execute("UPDATE fact SET valid_to=? WHERE id=?", (t2, fid_ab2))
conn_test.commit()

# (a) as_of=t1.5 区间内: A↔B 边在图里,BFS hops=2 从 Alpha2 达 Bravo2(1)+Charlie2(2)
g_in, _ = recall_mod._build_entity_graph(as_of=t1_5)
assert eb2 in g_in and ea2 in g_in and ec2 in g_in, (
    f"as_of={t1_5}: 图应含 A2/B2/C2 边(fid_ab2 区间内), got nodes={set(g_in.nodes())}"
)
neighbors_in = recall_mod.bfs_neighbors([ea2], g_in, hops=2)
assert eb2 in neighbors_in, (
    f"as_of={t1_5}: BFS 应达 Bravo2 (1-hop), got neighbors={neighbors_in}"
)
assert ec2 in neighbors_in, (
    f"as_of={t1_5}: BFS 应达 Charlie2 (2-hop), got neighbors={neighbors_in}"
)
res_in = recall_mod.recall("Alpha2", use_bfs=True, bfs_hops=2, as_of=t1_5, boost=False)
ids_in = {f["id"] for f in res_in}
assert fid_ab2 in ids_in, (
    f"as_of={t1_5} (区间内): fid_ab2 应被 BFS 召回, got ids={ids_in}"
)
assert fid_bc2 in ids_in, (
    f"as_of={t1_5} (区间内): fid_bc2 应被 BFS 召回(Bravo2 1-hop/Charlie2 2-hop), got ids={ids_in}"
)
print(f"✓ 9a. as_of+use_bfs 区间内: fid_ab2 & fid_bc2 recalled, ids={ids_in}")

# (b) as_of=t3 已失效: A↔B 边被时间过滤掉,A2 变孤立,BFS 从 Alpha2 不达 Bravo2/Charlie2
# (fid_bc2 无 valid_to 仍有效 → B2/C2 留在图中, 但 Alpha2 只有一条 A↔B 边, 该边失效
#  后 Alpha2 脱离图 → bfs_neighbors 从 Alpha2 返回 {})
g_out, _ = recall_mod._build_entity_graph(as_of=t3)
assert not g_out.has_edge(ea2, eb2), (
    f"as_of={t3}: 图应丢 A2↔B2 边(fid_ab2 已失效), got edges={list(g_out.edges())}"
)
neighbors_out = recall_mod.bfs_neighbors([ea2], g_out, hops=2)
assert eb2 not in neighbors_out and ec2 not in neighbors_out, (
    f"as_of={t3}: BFS 不应达 Bravo2/Charlie2 (A↔B 边被时间过滤), got neighbors={neighbors_out}"
)
res_out = recall_mod.recall("Alpha2", use_bfs=True, bfs_hops=2, as_of=t3, boost=False)
ids_out = {f["id"] for f in res_out}
assert fid_ab2 not in ids_out, (
    f"as_of={t3} (已失效): fid_ab2 不应召回(valid_to < as_of), got ids={ids_out}"
)
assert fid_bc2 not in ids_out, (
    f"as_of={t3} (已失效): fid_bc2 不应召回(Bravo2 不可达), got ids={ids_out}"
)
print(f"✓ 9b. as_of+use_bfs 已失效: fid_ab2 & fid_bc2 excluded, ids={ids_out}")

# (c) 对称验证: use_bfs=False + as_of=t1.5 → fid_bc2 不召回(门控,断言 BFS 路径是召回来源)
res_nobfs_in = recall_mod.recall("Alpha2", use_bfs=False, as_of=t1_5, boost=False)
ids_nobfs_in = {f["id"] for f in res_nobfs_in}
assert fid_bc2 not in ids_nobfs_in, (
    f"use_bfs=False: fid_bc2 (图近 fact) 不应被召回(BFS 门控), got ids={ids_nobfs_in}"
)
print(f"✓ 9c. as_of+use_bfs=False 门控: fid_bc2 not recalled (BFS path is the source), ids={ids_nobfs_in}")

# (d) _temporal_clause 透传一致性: as_of 与 None 语义不同(契约对齐)
sql_none, params_none = recall_mod._temporal_clause(as_of=None)
sql_in, params_in = recall_mod._temporal_clause(as_of=t1_5)
assert params_none == [] and params_in == [t1_5, t1_5], (
    f"_temporal_clause 参数透传: None→[], as_of→[as_of,as_of]; got none={params_none} in={params_in}"
)
assert "valid_from" in sql_in and "valid_to" in sql_in, (
    f"_temporal_clause(as_of) SQL 应含 valid_from/valid_to 区间判定, got sql={sql_in!r}"
)
assert "status='active'" in sql_none, (
    f"_temporal_clause(None) SQL 应含 status='active' (default 零回归), got sql={sql_none!r}"
)
print(f"✓ 9d. _temporal_clause as_of 透传契约: None→status filter, as_of→valid_from/valid_to 区间")

shutil.rmtree(tmp)
print("\n✅ All BFS recall + gating tests passed.")
