# mem-service 迭代 log

Date: 2026-08-07 · main HEAD: `094022a` · 12 ADR · ~40 pytest · KG structured recall v1→v6 + 修正

## 概述

mem-service 是 **agent-os-v2 (AO2)** 的 KG-based 结构化记忆服务, 叠加在 Claude Code file-based memory (MEMORY.md) 之上 (**双轨, 不改 CC MEMORY.md**, ADR-15 单向投影)。技术栈: Entity+Fact reified KG (SQLite) + 蝴蝶翼 LLM 抽取 + LIF 五维 + type-aware decay + pagerank centrality + 向量召回 + PreCompact autoDream + KG init bootstrap + 分布式 index。

**架构**: 纯脚本 CLI (无 daemon) + SQLite 跨进程持久 + PreCompact hook (bash) 触发。

---

## 迭代 (按时间正序)

### v1 (`cd4dcc9`) — KG 基础
commits: `2385e68` skeleton(schema+store) + `b03aaec` recall + `fe7bdf0` ingest+regex + `85cc5c5` consolidate + `0596c16` skill + `61fa048` e2e + `dfb7213` P3-fix + `cd4dcc9` spec/ADR + `60fea34` P0 deploy
- **ADR-1** cli seam · **ADR-2** Entity+Fact reified (无 MemoryItem) · **ADR-3** Fact self-contained · **ADR-4** score=match×lif · **ADR-5** regex extractor (7英文谓词+中文同义+9模式)
- 11 文件 +1187 行: cli/db/store/schema.sql/extractor/recall+scoring/consolidate/skill/tests(6 passed)
- deploy: `~/.claude/skills/mem` → `services/memory-service` (软链)

### v2 (`d7f8058`) — decay + pagerank + baseline
commits: `ce4d629` decay + `0a46146` baseline + `d7a68ef` pagerank + `d7f8058` P3-fix
- **ADR-8** type-aware decay (idempotent, `original_lif` 重基, half_life ephemeral=7d/stable=90d/permanent=∞) · **ADR-2v2** networkx pagerank on-the-fly · **ADR-4v2** score 加权 α·match+β·centrality+γ·LIF · **ADR-9** baseline (eval_recall hit@k)
- baseline 实证盲区: synonym/rewrite hit@5 **0%** (字面 m=0), 角点印证对权重无解

### v3 (`00fdf10`) — adapter + LIF 五维 + ScoreTune
commits: `e27bb56` schema+8列 + `da96fa1` adapter + `a40f1d4` compute_lif + `7a61e41` A-fix + `2781400` C-fix + `dfd3c48` E-skill + `c0ad3db` E-fix + `87fdb55` D-fix + `40fe76c` P3-fix
- **ADR-5b** adapter 蝴蝶翼 (LLMProvider Protocol + CCRProvider + N=3 fan-out + 投票 quorum + regex fallback + merge voted) · **ADR-8v2** LIF 五维 (freq/recency/spread/coherence/source) · **ADR-4v2** ScoreTune (调参实证盲区对权重无解, 真解=向量/LLM)
- 15 commits +1752, 调参增益=0 (默认最优)

### v3b (`1a90939`) — PreCompact autoDream hook
commits: `b7f237d` autodream + `18412d8` PreCompact hook + `1a90939` docs
- **ADR-10** PreCompact autoDream (session transcript→KG 增量, ADD/UPDATE/DELETE/NOOP + 幂等, exit 0 放行 compact)
- agent 自主召回 (非 UserPromptSubmit/SessionStart 注入, 用户决策)
- hook settings.json 注册 (matcher *, canonical abs path)
- 实测: 本会话 `/compact` 触发 hook 端到端 (stdout "PreCompact [...] completed successfully")

### v4 (`c393cec`) — 蝴蝶翼 LLM 接入 autodream
commit: `a22e70d`
- **ADR-11** autodream 切 `adapter.extract_facts` (N=3 蝴蝶翼 + 投票 + regex fallback), `fact.extractor` llm/regex 标注, cli autodream `--regex`
- 实测价值: 同文本 LLM **4 干净 fact** (conf 0.70: MemoryService/蝴蝶翼/PreCompact hook) vs regex 2 (含 `yService` 垃圾, 0.50)
- 11 pytest (mock LLM + cli --regex) + 27 回归

### v5 (`2350b0d`) — KG init bootstrap
commit: `7e8c26e`
- **ADR-12** cli init-memory (CC memory .md → KG permanent 种子, autodream 复用 DRY), autodream `fact_type` 参数
- 实测冷启动: 22 CC memory .md → **72 fact** (llm 46 + regex 26), recall 端到端命中

### v6 阶段 1 (`07f1e5c`) — embedding provider
commit: `c2fe79e`
- **ADR-13** embedding.py (EmbeddingProvider Protocol + OpenAICompatEmbedding, LM Studio/Ollama, passive + cache)
- 基础设施实测: LM Studio **16666** (非默认 1234, `lms load`), Ollama 11434 (qwen3-embedding:4b), CCR 3456 不支持 embedding (404, 仅 LLM)
- vec baseline (nomic): blind 14.3% (字面 0%)

### v6 阶段 2 (`cea7557`) — recall 向量召回融合
commit: `ded4deb`
- recall.py `use_vec` 向量候选扩展 (VEC_MIN 0.30/VEC_TOP_N 20) + scoring δ·vec_sim 维 (DELTA_VEC 0.3) + cli recall `--vector`
- 46 pytest (test_vec_recall 4 mock) + eval_recall 不破

### 修正: qwen3-4b 默认 (`e9636bd`)
- **纠错**: 早期 9 对 cosine 小样本选 nomic (qwen3 syn 0.575≈irr 0.566 "重叠"), 完整 vec baseline 证伪 — **qwen3-4b blind 57.1% 碾压 nomic 14.3%**
- embedding.py 默认 nomic → qwen3-embedding-4b
- **教训**: cosine 绝对值 ≠ 相对排序, 选模型必跑 hit@k baseline
- recall --vector 融合: blind **0% → 57.1%** (qwen3-4b)

### ZhipuAnthropicProvider (`b7eda40`)
- llm_provider.py ZhipuAnthropicProvider 直连智谱 (`open.bigmodel.cn/api/anthropic`, glm-5-turbo, coding plan), 不经 CCR (少一跳)
- api_key 从 env `ZHIPU_API_KEY`/CCR config 复用 (**不进 git**), 国内服务禁境外代理 (`ProxyHandler({})`)
- adapter.default_providers: `[Zhipu, CCR fallback]`
- 实测蝴蝶翼 3.3s conf 0.70

### ADR-14 source_cwd b 方案 (`eca8d30`)
- schema fact 加 `source_cwd` TEXT + idx, db.init migration (老 db ALTER ADD)
- autodream/recall/cli/hook/bootstrap 透传; recall `--cwd` 过滤 (OR NULL 召回兼容); ingest `source_cwd=os.getcwd()`; hook 读 stdin cwd 传 cli `--cwd`
- 解单体 KG 跨 cwd 混合缺陷 (**单体 + cwd 标签**, 非 per-cwd 库)
- 46 pytest + cwd 过滤烟测

### 向量持久化方案 A (`245e711`)
- embedding.py two-tier cache: **L1 内存 + L2 SQLite** (`data/embeddings.db`, text_hash→vector JSON, INSERT OR REPLACE 即时持久)
- 解 cli 短命 cache 不累积 (每次新进程重 embed 的真问题)
- 方案 A (用户选, 非 daemon B): SQLite 即时持久
- 烟测: L1/L2 hit 都不调 provider (L2 跨进程持久)

### ADR-15 分布式 index (`a2632cb`)
- projection.py: KG 高 LIF fact → CC memory `mem-<fact_id>.md` (实体文件, CC Read/description 召回工作) + MEMORY.md append/update `[mem]` 索引行 (幂等, CC 原生 append/update 嵌入)
- 散 index 标记: frontmatter `source: mem-service` + 索引行 `[mem]` 区分 CC 原生
- 双指向混: 文件 link (CC Read) + `kg://fact/<id>` (mem 召回)
- cli build-index `[--scope --top-k --memory-dir]`, PreCompact hook autodream 后**硬编** build-index

### fix build-index 严格 cwd (`0cbef5c`)
- build_index SQL: `source_cwd=? OR NULL` → **严格 `source_cwd=?`** (NULL 老数据不投影, 不混 cwd)
- **recall --cwd OR NULL** (召回兼容老数据不丢) vs **build-index 严格 = cwd** (投影纯), 有意非对称
- 烟测: 多 cwd 各自聚合 (cwd1=[rust] cwd2=[python]), NULL (LIF 0.9 最高) 不投影任何 cwd

### P0 schema 图盲修复 (`8fa5c9a`) — 2026-08-08
- **根因**: 旧扁平 FactOut 把 object 当字符串值, store 只对 subject 建 entity → object_id 恒 NULL → 零 entity↔entity 边 → 图密度 0(75% 孤儿)。schema 图盲, 非 prompt 措辞问题。
- **schema 两段**: EntityOut{name,type,aliases} + EdgeOut{subject,predicate,object} 替 FactOut;_EXTRACT_PROMPT 两步(先声明 entities, 再在已声明实体间抽 edges, object 永远是另一实体);_parse_facts 解析 entities+edges + dangling edge 过滤。
- cli.ingest/autodream: subject+object 都 _ensure_entity → put_fact(object_id) 必非空;entity_type 真传入(删两处 "inferred" 硬编码)。adapter._vote 新形状重写。
- 自检 fact_total=2/object_id 非空=2/边=2, 7/7 测试过。(Pi 研究 R1 Tier 1, docs/research/kg-graph-building-research.md)

### projection-native-format (`780d1dd` + merge `3f56163`) — 2026-08-08 [orchestrator 全流程 green]
- **事故止血**: 两次严重事故根因 = 实现 bug(测试假绿), **非设计错**。
  - issue 1: 文件名严格匹配三法不一(严格正则 / frontmatter source / frontmatter fact_id)+ 创建侧不校验 → 投影/native 混淆(历史 mem-service-* 被 startswith 误判, test_prune T5 记录)。
  - issue 2: MEMORY 索引行非原生格式([mem] 标记 + score/kg:// 机器向摘要 + memory/ 错前缀)→ CC 据明文摘要召回不到。
- **ADR-A 投影纯原生格式**: `_format_mem_line` → `- [{topic}](mem-{4hex}-{slug}.md) — {topic}`;`project_fact_md` description = 干净 topic(删 "mem-service KG fact, LIF" 噪声);正文自包含不回查 KG。
- **ADR-B 文件名严格契约**: `MEM_FILE_RE = ^mem-[0-9a-f]{4}-.+\.md$` 全仓唯一常量, 创建(_mem_filename assert raise)/识别(synthesis_index)/清理(bootstrap.prune)三处共用;文件名可读 `mem-{fact_id[:4]}-{slug}.md`(非 32-hex)。
- **ADR-C EdgeOut.topic**: LLM 抽取时生成一句话事实概括, 贯通 adapter._vote/cli/autodream, **持久化 fact 表 topic 列**(db.init ALTER ADD 老 db 迁移)。
- **关键认知(推翻早期 ADR-19 草案)**: CC 召回是**消解型**(MEMORY 全量注入 + frontmatter description 代码层召回 + Read 工具), 非 prompt 驱动 → mem-service **不需 hook 注入 context**, 只需产原生相容记忆文件 + 索引, CC 自身机制捞。project-back 方向正确, 错在格式非原生。证据: reference-cc-memory-mechanism.md。
- orchestrator P0→P4 green(P1 grill/ADR → P2 workflow(implement+skeptic+fix) → P3 回归 7/7 + ADR 3/3 对照 + 全分支审查)。15 文件 +312/-146。
- **清理副作用**: 37 个旧投影文件(memory-service 21 + agent-os-v2 16)+ KG db 119 行旧 schema 垃圾全清, native memory md5 验证零受损。
- skeptic 2 minor(MEM_FILE_RE 理论 collision 面, source frontmatter 兜底 / _sanitize_slug 未替反斜杠 Linux-only), 接受权衡。
- **defer(记 ADR 方向, docs/adr/projection-native-format.md)**: D3 实体解析共指消解(Graphiti dedupe_nodes)/ D4 双时态 valid_at / **D5 BFS 召回(KG 唯一独占价值, 下一个迭代)** / D6 gating(避单 session -17.7%)/ D7 aliases 持久化。
- 踩坑: orchestrator render-workflow-script 没 map `verify.general_test.run`→`general_test_cmd`(空), 需手动补否则 skeptic 不跑测试;orchestrator-state.json 残留(上迭代卡 P3)要先 init-state 重置。

### entity-dedupe-aliases (D7 别名 + D3 两步合并) — 2026-08-08 [OMP 实施 + 主 session 2 轮 review/fix]
- **治孤儿实体根因**: P0 边出现后, 跨 ingest 同实体不同写法(A2A/a2a、native agent/原生 agent)各建独立 entity。本迭代加两步共指消解(Graphiti dedupe_nodes 风格)+ 别名持久化。base `0603427`(projection 加固 F1-F7 之后)。
- **Node A — D7 别名持久化**: entity 表加 `aliases`(JSON TEXT default '[]')+ `name_embedding`(JSON TEXT);`store.put_entity` 存别名(embedding 不在 store 自动算, 由 resolver 算一次显式传入, skeptic 修正避污染 embeddings.db + 防火墙 block);`store.find_entity_exact` 大小写不敏感精确匹配(name 或任一 alias 完全相等)廉价闸。
- **Node B — D3 两步合并** (`resolver.py` 新): step1 `find_entity_exact` 命中即返回 + 并入新别名(surface form 记入 alias, 与 step2 对称);step2 `embedding.embed` 余弦 top-k5 候选 → `ZhipuAnthropicProvider.dedupe_entity`(few-shot: Java语言≠Java岛 绝不合 / NYC=New York City 合, NEVER mark related-but-distinct as duplicate)→ 命中合并 / 未命中新建。cli.ingest + autodream subject+object 双侧改用 `resolver.resolve_entity`(替 `_ensure_entity`), resolved id 流进 fact.subject_id/object_id。
- **D1 root-cause fix(2 轮 review 逼出)**: 离线(embedding 不可达)期建的实体 name_embedding 永久 `[]`, `_cosine_topk if not vec: continue` 跳过 → 同义异写走不到 step2 → LLM 从不被调用 → 建第二个孤儿(**结构性盲区, 非边角**)。修法: `_cosine_topk` 对 emb=[] 既有实体用当前 `embedding_providers` 惰性 re-embed + `store.backfill_entity_embedding` 幂等回填(`WHERE name_embedding IS NULL OR name_embedding='[]'` — **双认 NULL(老库迁移行)/'[]'(离线 INSERT 行)**, 只写 '[]' 漏老库), 让其成为正常候选; step1/step2 命中也调回填。
- **验证(三重独立)**: ① ultracode review(6 维度→对抗核查→综合)22 发现 11 确认→去 8, must-fix=D1+T1;② fan-out reverify(8 独立 lens 默认反驳各自复现)7 confirmed + 1 partial, 0 反驳, D1 4/4 + T1 定性"代码对、仅测试缺口";③ fix 工作流(implement→skeptic+runner→fix loop)round 2 green + 主 session 新鲜跑 **11/11**(test_resolver 16 case 含 Test16 D1 回归 toggle 验证 / test_integration_resolver T1 接线)。data/memory.db 零污染。
- **T1 集成测试** (`test_integration_resolver.py`): cli.ingest 真接线同实体异写两次 → 断言 count==1 无孤儿 + fact.subject_id/object_id 都 IS NOT NULL 且==resolved id + aliases 含异写。补上才覆盖 Node B"合并后 fact 指向 resolved id"验收(项目头号雷区"测试假绿"典型缺口 — 单测 resolver 不够)。
- **顺手清(6 minor/nit)**: T2 spec Node A 对齐 / R2 删 `dedupe_entity` dead `context` 参数(resolve_entity 本无 context, 6 调用点全没传, prompt 空行噪音)/ T3 补 phantom-id + 维度不匹配两守卫测试 / D2 test monkeypatch `_CACHE_DB` 指 tmp 不开生产 embeddings.db / D4 EntityOut docstring 更新 / R3 `find_entity_exact`+`_cosine_topk` 加 `ORDER BY created_at`。
- **踩坑**: ① 老 db `ALTER ADD COLUMN name_embedding TEXT` 无 DEFAULT → 既存行 NULL(非 '[]'), 回填 WHERE 必须双认, blast radius=全部历史 entity(当前库空=前向风险);② `object_id nullable + FK` 不挡 NULL → 删 object resolver 突变静默落地坏数据, 集成测试须断言 object_id IS NOT NULL;③ 测试假绿: 单测 resolve_entity 不覆盖接线(fact→resolved id), 要集成测试;④ OMP 实施但未留 P3 自评(信主 session 独立 review, 不信未验证完成声明)。
- **defer(本迭代治 D3+D7)**: D5 BFS 召回(KG 唯一独占价值, 下一迭代)/ D6 gating(避单 session -17.7%)/ D4 双时态。盲区: embedding 模型升级维度变→全库 name_embedding 失效无迁移; aliases 只加不删无 GC; 并发 re-ingest 无 UNIQUE(name,type) 竞态。

### bfs-recall-gating (D5 BFS 召回 + D6 门控) — 2026-08-08 [OMP 实施 + 主 session 验证]
- **D5 BFS 召回**: KG 相对纯向量 RAG 唯一独占价值(φ_bfs 抓「语义远但图近」, R2 实证 +18.5% LongMemEval)。recall.py 抽 `_build_entity_graph() -> (nx.Graph, centrality)`(图复用单一源, centrality + BFS 共用, 不建两次);新增 `bfs_neighbors(seeds, graph, hops=2, max_nodes=50)`(nx 单源最短路取 min hop, lowest-hop-first 截 max_nodes)+ `_hop_decay`(0→1.0/1→0.5/2→0.25)。recall() 加 `use_bfs=False, bfs_hops=2`: seed entity → BFS → 邻域 fact 并入候选, BFS-found fact(hop>0)**绕过 score≥0.3 硬滤**(显式图召回非噪音)仍参与排序。
- **D6 门控 = opt-in `--bfs`**(default off): 契合项目"agent 自主召回"哲学; -17.7% 单 session 退化源于 BFS 总 fire, opt-in 天然规避; **default off 逐字零回归**(score_fact 加 bfs_proximity=0 → 同分; 过滤线 `>=0.3 or fid in bfs_expanded_ids`, use_bfs=False 时空集 → 同 `>=0.3`)。cli `--bfs` / `--bfs-hops` flag。
- **scoring**: score_fact 加 `bfs_proximity` 形参 + `BFS_WEIGHT=0.1`(ε), `score += BFS_WEIGHT·bfs_proximity`(镜像 `DELTA_VEC·vec_sim`);**weights (α,β,γ) 三元组不动**(eval_recall grid 依赖)。
- **验证**: OMP(workflowz)实施; 主 session 新鲜跑 **12/12**(含 test_bfs_recall 9 断言: BFS 召回 2-hop fact / 门控 off 不召回 / hop cap hops=1 vs 2 / max_nodes≤50 / hop_decay / zero-regression bfs=0.668≥without=0.618 / db 隔离)。data/memory.db 零污染。P0+projection+entity-dedupe 契约未触(只加可选路径, default off 不改原 recall)。
- **踩坑**: ① OMP 终端歧义 —— 两个 "OMP ready" 都在 agent-os-v2 cwd, 主 session 误猜 omp2(劫持自其 agent-os-v2 ao-tui 任务); 但 **OMP 框架按消息目标路径绝对路径编辑**, BFS 改动正确落 /home/yy/projects/memory-service(agent-os-v2 副本 git 干净未碰)。下次分派前先实时确认唯一 OMP 身份, 不靠排除法猜。② omp2 做完回 agent-os-v2, 上下文混了(BFS diff 无污染, 可接受)。
- **defer(本迭代治 D5+D6)**: D4 双时态 valid_at/invalid_at(Graphiti bi-temporal 边失效, 下一迭代)。BFS auto-suggest hint(direct-match 薄时提示 rerun --bfs)/ 跨 cwd BFS 门控 / BFS_WEIGHT baseline 调参 / BFS+use_vec 组合深测。

---

## ADR 清单 (12 活跃)

| ADR | 决策 | 文件 |
|---|---|---|
| ADR-1 | cli seam (Python + argv 同管道) | mem-service.md |
| ADR-2 | Entity+Fact reified KG (无 MemoryItem, SQLite) | mem-service.md |
| ADR-3 | Fact self-contained (value literal + object_id 可选) | mem-service.md |
| ADR-4v2 | score = α·match+β·centrality+γ·LIF+δ·vec_sim (supersede ADR-4 match×lif) | mem-service-v2/v3/v6.md |
| ADR-5 | regex extractor (中文同义+英文模式, fallback) | mem-service.md |
| ADR-5b | adapter 蝴蝶翼 LLM (N=3 投票 + regex fallback) | mem-service-v3.md |
| ADR-8v2 | LIF 五维 (freq/recency/spread/coherence/source) + decay idempotent | mem-service-v2/v3.md |
| ADR-9 | recall baseline (hit@k 量化盲区) | mem-service-v2.md |
| ADR-10 | PreCompact autoDream (session→KG 增量, exit 0 放行) | mem-service-v3b.md |
| ADR-11 | 蝴蝶翼 LLM 接入 autodream (regex 降 fallback) | mem-service-v4.md |
| ADR-12 | KG init bootstrap (CC memory→KG permanent, autodream 复用) | mem-service-v5.md |
| ADR-13 | 向量层 (embedding provider + recall --vector 融合 + SQLite L2 cache) | mem-service-v6.md |
| ADR-14 | source_cwd b 方案 (单体 KG + cwd 标签, recall --cwd 过滤) | (commit eca8d30) |
| ADR-15 | 分布式 index (KG→CC memory 投影, [mem] 标记, 真嵌入) | (commit a2632cb) |

(ADR-4 被 4v2 supersede; ADR-8 被 8v2 演进)

---

## 当前状态 (`main 094022a`)

**存储**:
- KG: `data/memory.db` (entity 5 列 + fact 25 列含 source_cwd, SQLite WAL)
- 向量 cache: `data/embeddings.db` (embed_cache, L2 SQLite, text_hash→vector)
- CC 投影: `~/.claude/projects/<encoded-cwd>/memory/mem-<fact_id>.md` + MEMORY.md `[mem]` 索引行

**Provider**:
- LLM 抽取 (蝴蝶翼): ZhipuAnthropicProvider (glm-5-turbo 直连智谱) + CCR fallback
- Embedding: LM Studio qwen3-embedding-4b (port 16666) + Ollama fallback

**测试**: ~40 pytest (test_e2e/lif_wire/lif_coherence/adapter/autodream/bootstrap/vec_recall) + eval_recall 2 (ADR-9 baseline + ADR-4v2 grid)

**架构**: 纯脚本 CLI (cli.py argparse, 短命进程) + SQLite 跨进程持久 + PreCompact hook (bash) 触发。无 daemon/server/port。

---

## defer 队列 (10)

1. autoDream daemon (CC server-side flag `tengu_onyx_plover` 未开)
2. 中文 embedding 调优 (BGE-M3, qwen3-4b 已强 57.1%)
3. fact on-ingest 预计算 embedding (避免 on-recall)
4. 跨 scope memory 向量联邦 (多 CC project)
5. memory 变更增量检测 (mtime, 全量幂等重跑够)
6. 新 LLM/embedding provider (claude-api/LMStudio extract, 当前够)
7. 冷层归档 (依赖向量, KG 小不需)
8. CC→KG 反向 re-ingest (用户编辑 memory/*.md → KG)
9. SessionStart build-index hook (new 触发投影)
10. daemon (进程常驻 L1 累积/pagerank 持久, 边际延迟优化非功能缺口)

**已否定** (用户决策): UserPromptSubmit 自动注入 / SessionStart(compact) 自动注入 — agent 自主召回 (recall 结果在 context, 非主动注入)。

---

## 关键设计决策

- **双轨** (mem-service KG + CC MEMORY.md, 不改 CC, ADR-15 单向投影)
- **agent 自主召回** (非自动注入; recall 结果在 context, 用不用 agent 决定)
- **PreCompact hook 抢救 session** (compact 前 autodream, session 压缩丢原文前落 KG)
- **单体 KG + source_cwd 标签** (非 per-cwd 库, ADR-14 b 方案; recall 宽松 OR NULL / 投影严格 = cwd)
- **纯 CLI + SQLite** (非 daemon, 方案 A 跨进程持久; L1 进程内 + L2 SQLite)
- **蝴蝶翼 LLM** (N=3 投票 + regex fallback, ADR-5b; 防 LLM 幻觉 + 中文盲区)
- **LIF 五维** (freq/recency/spread/coherence/source, recall boost 滚动累积 = 持久 importance)
- **分布式 index** (recall 散 index 在 context + MEMORY main index new/precompact 周期 update, ADR-15; 真嵌入 CC 文件指向 + kg:// 虚拟补充)
- **score query-dependent 不持久** (实时算, LIF 是持久信任标量)
