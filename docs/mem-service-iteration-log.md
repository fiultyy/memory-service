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
