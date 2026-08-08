# Spec: bfs-recall-gating(迭代 3:D5 BFS 召回 + D6 门控)

- **base**: `50402ef`(entity-dedupe-aliases 之后, p0-entities-edges-schema 分支)
- **项目**: `/home/yy/projects/memory-service`(Python, 纯 stdlib+urllib+networkx, 无框架)
- **背景**: P0(schema 边)+ entity-dedupe(实体合并 + name_embedding 回填)已让 entity↔entity 边真实存在且连通。recall.py 现仅字面 match + 向量召回(use_vec)+ pagerank centrality + LIF,**无图遍历**。D5 = 加 n-hop BFS 召回(KG 相对纯向量 RAG 的唯一独占价值);D6 = 门控(避单 session 退化)。

## 研究证据(docs/research/kg-harness-emergence-deepdive.md)
- L66-67: 召回 = α·φ_cos + β·φ_bm25 + γ·φ_bfs;**φ_bfs 抓「语义远但图近」**(查 A2A → BFS 拉共现 mesh/node 实体及边, 向量距离远但拓扑邻接), 是 KG 相对纯向量 RAG 唯一独占贡献。
- L197/212-215: Graphiti n-hop BFS(**hops=2**)+ bi-temporal + 同实体对边 dedupe, 直接解释 +18.5% LongMemEval。落地: recall 加 `bfs(entity_id, hops=2)` → 邻域 fact, 与向量召回 fusion。**前提 = 边存在(P0 档 1, 已完成)**。
- L63: 召回注入建议 ≤20 fact, 按 LIF×centrality 截断。

## 已锁决策
- **门控 = opt-in flag**: BFS 默认 OFF(`use_bfs=False`), cli `recall --bfs` 显式开启。理由: (1) 项目哲学"agent 自主召回, 非自动注入"(recall 结果在 context, 用不用 agent 决定; ADR 已否定 UserPromptSubmit/SessionStart 自动注入);(2) 单 session -17.7% 退化源于 BFS 总是 fire 污染浅查询, opt-in 天然规避;(3) **零回归**(default off = 现有 recall 行为逐字不变)。
- **BFS = 候选扩展 + bfs_proximity 计分**(φ_bfs fusion 的 ponytail 实现, 非独立召回路径): 从 seed 实体(search_entities 锚定的)沿 on-the-fly 图 BFS N hop → 邻域实体 → 其 fact 并入候选池;BFS-found fact 加 `bfs_proximity` 分项(hop decay: hop0=1.0 / hop1=0.5 / hop2=0.25)。
- **BFS 扩展 fact(hop>0)不受 score≥0.3 硬滤**(它们是显式图召回非噪音), 但仍参与 score 排序(含 bfs_proximity), 最后 top_k 截断。这保证「字面/向量 match=0 但图近」的 fact 能被召回(正是 φ_bfs 价值)。
- **hops 默认 2**(Graphiti 实证), **max_nodes 上限 50**(防爆, 与 ≤20 注入上限同量级)。
- **图复用(单一源)**: recall.py 已在 `_build_centralities` on-the-fly 建 `nx.Graph`(每 recall 一次, 建完即丢只为 pagerank)。抽 `_build_entity_graph() -> (nx.Graph, centrality_dict)`, centrality + BFS 共用同一图, **不建两次**。
- **bfs_proximity 集成**: 镜像 `vec_sim`/`delta` 模式 —— `scoring.score_fact` 加 `bfs_proximity: float = 0.0` 形参 + 模块常量 `BFS_WEIGHT`(ε);`score += ε·bfs_proximity`。**weights (α,β,γ) 三元组不动**(eval_recall grid 依赖)。

## Node A — D5 BFS 召回(核心)
**改**:
- `recall.py`: 抽 `_build_entity_graph() -> tuple[nx.Graph, dict[str,float]]`(从 active fact 建无向 entity 图 + pagerank centrality, 返回两者);`_build_centralities` 改调它取 centrality(单一源, 不重复建图)。
- `recall.py` 新增 `bfs_neighbors(seed_entity_ids, graph, hops=2, max_nodes=50) -> dict[str,int]`: BFS 遍历返回 `{entity_id: 最近_hop}`(seed hop=0, 不超 hops, 不超 max_nodes 个)。空图 / 无 seed → {}。
- `recall()` 加 `use_bfs: bool = False, bfs_hops: int = 2` 形参。use_bfs 时: 现有 seed entities → `bfs_neighbors` → 邻域实体 → `_facts_for_entities` 扩展候选(每 fact 标 min_hop = 其 subject/object 实体的最小 hop);score 阶段传 `bfs_proximity = hop_decay(min_hop)`(hop0=1.0/1=0.5/2=0.25, 无 hop=0.0)。BFS 扩展 fact(hop>0)绕过 score≥0.3 硬滤, 仍排序 + top_k。
- `scoring.py`: `score_fact` 加 `bfs_proximity: float = 0.0` 形参 + `BFS_WEIGHT` 常量;`score += BFS_WEIGHT * bfs_proximity`(镜像 `delta * vec_sim`)。weights 三元组不动。默认 bfs_proximity=0 → 现有调用零变化。

**验收**: BFS 能召回「字面/向量 match=0 但图 ≤2-hop 内」的 fact;图复用单一源(一次建图);score_fact 新形参默认 0 → 现有 recall 不变;现有 11 个 test 过 + 新 BFS 测试过;db 隔离零污染。

## Node B — D6 门控 + cli 接线 + 测试(依赖 A)
**改**:
- `cli.py`: recall 子命令加 `--bfs` flag(→ `use_bfs=True`)+ 可选 `--bfs-hops`(默认 2, dest bfs_hops);透传 `recall_mod.recall`。
- `recall.py` 模块/函数 docstring 更新 pipeline(加可选 BFS 路)。
- `test_bfs_recall.py`(新, db.init(tmp) 隔离):
  - 构小图: fact(A, uses, B) + fact(B, runs_on, C)。query="A", `use_bfs=True` → 断言 C 的 fact(2-hop, query 字面/向量都不命中 C)在结果里;`use_bfs=False`(default)→ C 的 fact **不在**结果(门控: BFS off 不 fire)。
  - hop cap: `bfs_hops=1` → C 不召回(B 召回, C 在 1 hop 外)。
  - max_nodes cap: 构 >50 实体的星图 → `bfs_neighbors` 返回 ≤50。
  - **零回归铁证**: `use_bfs=False` 时 recall 结果与现状逐字一致(门控 default off)。
  - 真断言副作用(BFS-found fact 真在结果 list 里, 查其 fact_id), 非假绿;跑前后 `data/memory.db` entity count 不变。

**验收**: `cli recall --bfs` 端到端;门控 default off 零回归(BFS off 时 11 个现有 test 仍逐字过);BFS 测试过;db 隔离。

## 约束
- **不破现有 recall**: `use_bfs=False`(default)时 recall 行为逐字不变 —— 现有全部 test_*.py 过。
- 不破 P0 {entities,edges} + projection-native-format + entity-dedupe 契约(MEM_FILE_RE / 原生索引 / topic / resolver / name_embedding backfill)。
- 不破 scoring weights (α,β,γ) 三元组(eval_recall grid 依赖)。
- 测试隔离 db.init(tmp), 零污染 data/memory.db。
- **不 commit**(主会话做)。

## 后续迭代(本 spec 不含)
迭代 4 = D4 双时态 valid_at/invalid_at(Graphiti bi-temporal 边失效, R2 L197)。defer: BFS auto-suggest hint(direct-match 薄时提示 rerun --bfs)、跨 cwd BFS 门控、BFS_WEIGHT 调参 baseline。
