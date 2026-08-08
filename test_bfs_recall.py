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

shutil.rmtree(tmp)
print("\n✅ All BFS recall + gating tests passed.")
