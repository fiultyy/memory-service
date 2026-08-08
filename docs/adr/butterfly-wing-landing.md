# ADR-18: 蝴蝶翼(Butterfly Wing)原理念落地 — 建图 + 扩散(debt)
Date: 2026-08-08
Status: **Debt / Deferred**(厘清理念 + 落地方向定稿,实现待 Phase A 起排期)

## Context

### 同名歧义(本 ADR 的由头)
"蝴蝶翼"在系统里是**两个不同的东西**,长期被混用:
- **原版**(AO2 `services/orchestrator/src/memory/butterfly_wing.py` + `docs/memory-kernel-design.md §4.5`):**双向联想激活机制** —— Forward Wing(归纳/扩散:input→generalization)+ Backward Wing(锚定/收敛:generalization→concrete)+ Composite。是 KG 图上的**扩散边权重 / 联想传播路径**,与 LIF 融合成**神经状态场**(LIF=激活电位 / KG=网络结构 / 蝴蝶翼=边权重;Collins & Loftus 1975 spreading activation)。§4.5 原文:"蝴蝶翼=KG 图上的扩散边权重,**不是给单条记忆评分的工具**"。
- **借名**(mem-service `adapter.py` ADR-5b):N 路 LLM 抽取 fan-out + 投票 + confidence 聚合。**完全不同的东西**,只是借了名字。

mem-service 只 cp 了**借名的 adapter** + 把 LIF **降维成 per-fact 标量**(ADR-8v2,丢了"概念电位场/扩散"语义)。**原版蝴蝶翼(扩散)从未移植**。AO2 里 butterfly_wing.py 本身也"未通电(空壳骨架,relevance 硬编码)"。

### 诊断:KG 不是图(ingest 效果差的真身,2026-08-08 实测)
| 指标 | 值 | 含义 |
|---|---|---|
| entities / active facts / distinct subject | 24 / 14 / 6 | fact 压在少数 subject 上 |
| 孤立 entity(0 active fact 引用) | **18/24=75%** | 抽取造的实体大半没接进图 |
| 实体图密度(object_id 非 NULL 的 fact) | **0.00** | **零 entity↔entity 边** |
| consolidate dedup | exact `(subject_id, predicate, object_key)` | 近义/变体不合并 |

根因:`cli.ingest` 只在**同一次 ingest 内** exact-name 匹配才设 `object_id`(`name_to_id.get(obj_name)`)——**跨 ingest 实体解析缺失**,边永远不积累;且多数 fact 是 `subject→literal值`("用户 uses rust",rust 非实体)。

→ KG 是 subject→literal 散点,**不是图**。蝴蝶翼/PPR 要扩散但**没边**——recall 的 `_build_centralities` pagerank 全 0。`mem_score` 里的 `confidence`(adapter 借名翼)也从未进 KG(put_fact 不传,恒 0.5),故 mem_score 现状 ≡ LIF 排序(见 P1 债)。

## Decision(落地方向,Deferred)

把**原版蝴蝶翼(扩散)**落在**值钱**的位置(recall/ingest),**不是** synthesis 排序(grill 已否:[mem] 几十行 + 零边 = 杀鸡牛刀且退化)。两翼 + 建图前提:

### 前提 + Backward Wing @ ingest(建图 + 治 ingest 差)
- adapter 抽 `(s,p,o)` 后,**跨 ingest 解析 o**:exact `find_entities_by_name` + embedding 近义 → 命中既有实体则设 `object_id` → **造边**。
- 边积累 → 孤儿实体接入图 → sprawl 降。**这步不靠 PPR 也先跑**(纯实体解析就改善 ingest)。
- 边多了再上 PPR-verify:从 s 扩散验证/反驳链接、矛盾检出、近义 dedup(超 exact-key)。
- 隐含:引导 extraction 多产 **object-实体**(adapter prompt 鼓励 object 指向已知实体,非 literal 短语)——否则图永远稀疏,两翼都跑不动。

### Forward Wing @ recall(用图,解字面盲区)
- recall 现候选 = entity-name LIKE + value 子串 → synonym/rewrite 全盲。
- 边存在后:PPR 从 query 命中实体扩散 → 图邻居 fact 纳入候选 → "rust" 经实体边联动召回 "铁锈/cargo" 相关 —— **非 LLM 路径**解盲区(比抽取 vote 便宜)。recall.py 已建图(centrality),加 PPR-neighbor 扩展复用。

### synthesis PPR —— 降级/删占位
- `MEM_SYNTH_PPR` 占位移除(零边 + 几十行 = 无收益)。spreading 预算挪到 recall/ingest。

## 分阶段

| 阶段 | 动作 | 依赖 PPR | 收益 |
|---|---|---|---|
| **A** | ingest 跨次实体解析(exact+embedding 链 o)→ 造边、清孤儿;adapter prompt 鼓励 object-实体 | 否 | 治 ingest 差,KG 成图 |
| **B** | recall 加 PPR-neighbor 候选扩展(Forward Wing) | 是(边已存) | 解字面盲区 |
| **C** | ingest/consolidate PPR-verify + 近义 dedup(Backward Wing 全) | 是 | 治碎片化、矛盾检出 |
| 清债 | 删 synthesis `MEM_SYNTH_PPR` 占位;put_fact 传 `confidence`(若仍要 mem_score 用 adapter 翼)或承认 mem_score≡LIF | — | 名实相符 |

## Alternatives considered
- **PPR 留 synthesis**:否(grill 已判 + 零边退化)。
- **全靠 LLM 抽取 vote(借名翼)解质量**:否(不建图,只是抽取置信,且 confidence 没进 KG)。
- **不动,承认 mem-service 是"flat KG + per-fact LIF"**:可接受的最小形态,但放弃原版蝴蝶翼的召回质量上限。

## Consequences
- Phase A 实现后 KG 才真正成图,recall centrality/PPR 才有意义(当前全 0)。
- 蝴蝶翼价值上限 ∝ 抽取的"关系化"程度(literal-heavy → 图稀 → 扩散收益有限)。Phase A 含 adapter prompt 引导 object-实体。
- 与 ADR-15(P2 synthesis)、ADR-8v2(LIF per-fact)并存:本 ADR 不改 LIF/synthesis,只补"建图 + 扩散"缺失层。

## Constrains
[ADR-5b adapter(借名翼,confidence 未接 KG)、ADR-8v2 LIF(per-fact,降维自原版场)、ADR-15 P2 synthesis(MEM_SYNTH_PPR 占位)、AO2 butterfly_wing.py + memory-kernel-design §4.5(原版,未通电)]
