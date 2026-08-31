# mem-service

> Claude Code 的记忆叠加层 — 在 file-based memory 之下加一层 KG 事实图谱。

mem-service 是一个独立 Python CLI（无 daemon / 无端口），为 Claude Code 提供结构化的长期记忆：把散落的对话事实与实体关系抽成 **Entity + Fact reified 知识图谱**，按需召回。它叠加在 CC 现有的 `MEMORY.md`（即时热层）之下，**不改 MEMORY.md**，仅在查询或 hook 触发时被读写。

- **形态**：argparse 短命进程 + SQLite 跨进程持久 + PreCompact hook 触发
- **依赖**：仅 `networkx`（图谱中心性）；LLM / embedding 为外部服务，经环境变量配置
- **零侵入**：CC 原生记忆不动；mem-service 是自治 KG + 单向投影

---

## 能力

| 能力 | 说明 |
|---|---|
| **蝴蝶翼 LLM 抽取** | N-way fan-out（默认 N=3 并行）+ 多数投票 quorum ⌈n/2⌉，case-fold 归一。抽取走 provider 抽象（Protocol），默认 Anthropic Messages 兼容协议直连，`base_url` / `model` / `key` 全经 env 配置；新增 provider 实现 `extract_facts` 即可接入 |
| **加权召回** | `score = α·match + β·centrality + γ·LIF + δ·vec_sim` 融合字面匹配、PageRank 中心性、LIF 信任标量、向量相似度 |
| **向量召回融合** | OpenAI 兼容 `/v1/embeddings`（本地优先，如 LM Studio / Ollama），解同义 / 改写 / 字面盲区；two-tier cache（L1 内存 + L2 SQLite） |
| **PreCompact autoDream** | CC `/compact` 前 hook 自动把 session transcript 抽成 KG 增量（ADD / UPDATE / DELETE / NOOP，幂等） |
| **LIF 五维 + type-aware decay** | freq / recency / spread / coherence / source 复合信任标量；按 `fact_type` half-life 幂等衰减（ephemeral 7d / stable 90d / permanent ∞） |
| **多值谓词共存** | functional（`is_a` / `belongs_to`）单值真矛盾 vs multivalue（`uses` 等）共存，不误 supersede |
| **KG → CC 投影** | 高 LIF top-K fact 单向投影回 CC memory（`[mem]` 标记 + 真嵌入），开局自动可见 |
| **source_cwd 隔离** | 单体 KG + cwd 标签，`recall --cwd` 过滤当前项目（兼容 NULL 老数据） |

---

## 架构

### 分层与数据流

```
Claude Code
  MEMORY.md  (热层 · 开局自动入 context)
      ↑  projection: KG → CC  (SessionStart synthesis-index 单点 + recall 驱动散 index 对账, 09-01 终裁A方案)
      │
mem-service (KG 叠加层)
      │
  cli ─┬─ ingest ───→ adapter   (蝴蝶翼 LLM: N-fan-out + vote)
       ├─ recall ───→ scoring   (α·match + β·centrality + γ·LIF + δ·vec)
       ├─ consolidate           (LIF decay + dedup)
       ├─ autodream ──────────┐ (session → KG 增量)
       ├─ init-memory ────────┤ (CC memory → KG permanent 种子)
       └─ synthesis-index ─────┘ (KG → CC 散 index 对账; SessionStart 单点触发)
                               ↓
                            store → db (SQLite)
                               │
  embedding: OpenAI-compat /v1/embeddings + two-tier cache (L1 mem + L2 sqlite)
  hook: PreCompact → autodream   (CC /compact 前自动触发)

  data/memory.db       (KG: entity + fact)
  data/embeddings.db   (向量 cache)
```

- **写入**：`ingest` / `autodream` → `adapter`（蝴蝶翼 N-wing 抽取 + 投票）→ `store.put_fact`（on-ingest 预计算向量入 L2 cache）→ SQLite KG
- **读取**：`recall` → 字面 match + pagerank centrality + LIF + 向量 sim 加权排序 → Fact 列表
- **巩固**：`consolidate` → LIF decay（half_life）+ 精确重复 supersede
- **投影**：`synthesis-index` → 扫散 `mem-*.md` → 回 KG 取 mem_score → 主题聚合(同 topic 唯一) → 对账重写 MEMORY.md 投影索引(ADR-A 原生格式, 无 `[mem]` 字面标记; SessionStart 单点自动 + cli 手动, MEMORY.md 投影行唯一写入口, ADR-15 P2 / 09-01 终裁A方案)

### 模块

| 模块 | 职责 |
|---|---|
| `cli` | 顶层 seam（argparse argv + Python 函数同管道） |
| `adapter` | 蝴蝶翼 LLM 抽取（fan-out + 投票 + confidence） |
| `llm_provider` | LLM Protocol + 具体实现（env 配置 base_url/model/key） |
| `store` / `db` | KG CRUD + SQLite schema(Entity + Fact reified) |
| `recall` / `scoring` | 召回入口 + 加权排序 |
| `resolver` | 实体解析(精确/别名闸 → 向量 top-k + LLM 去重 → 创建,ADR-D3) |
| `embedding` | OpenAI-compat 向量 + two-tier cache |
| `autodream` | session transcript → KG 增量(幂等) |
| `mem_daemon` | 常驻 autoDream loop(watch transcript 增长 → 增量 dream,operational #1) |
| `bootstrap` | CC memory → KG permanent 种子 |
| `consolidate` | LIF decay + dedup |
| `projection` | KG → CC memory 投影 |

---

## 设计决策（ADR）

| ADR | 决策 |
|-----|------|
| ADR-1 | cli seam（Python + argv 同管道） |
| ADR-2 | Entity + Fact reified KG（无 MemoryItem，SQLite） |
| ADR-3 | Fact self-contained（value literal + object_id 可选） |
| ADR-4v2 | score = α·match + β·centrality + γ·LIF + δ·vec_sim |
| ADR-5b | 蝴蝶翼 LLM（N-wing fan-out + 投票，无 regex 降级） |
| ADR-8v2 | LIF 五维 + decay idempotent |
| ADR-9 | recall baseline（hit@k 量化盲区） |
| ADR-10 | PreCompact autoDream（session→KG 增量，exit 0 放行） |
| ADR-11 | LLM 接入 autodream |
| ADR-12 | KG init bootstrap（CC memory→permanent 种子） |
| ADR-13 | 向量层（embedding + recall 融合 + L2 cache + on-ingest 预计算） |
| ADR-14 | source_cwd（单体 KG + cwd 标签） |
| ADR-15 | 分布式 index（KG→CC 投影，真嵌入） |

---

## 状态

**仍 defer**：冷层类聚 / 跨 scope 向量联邦 / query 独立 cli / BFS_WEIGHT 调参（需 eval）。

接线与部署见 [`INSTALL.md`](INSTALL.md)，CC 内调用语法见 [`SKILL.md`](SKILL.md)。
