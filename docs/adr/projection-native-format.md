# ADR: projection-native-format(投影原生格式 + 文件名严格契约 + topic 字段)
Date: 2026-08-08
Status: Active
Iteration: projection-native-format(base: 8fa5c9a,P0 schema 修复之上)

## 背景(事故根因,非设计问题)
两次严重事故的根因是**实现 bug(测试假绿)**,不是 project-back 设计错:
1. **issue 1**:recall/projection 的文件名严格匹配没作为统一契约硬编码 —— 三处识别用三种方法(严格正则 / frontmatter source / frontmatter fact_id),创建侧不校验。历史碰撞:native `mem-service-*.md` 被 `startswith("mem-")` 松散前缀误判为投影(test_prune T5 记录的真实 bug)。
2. **issue 2**:synthesis 写 MEMORY 索引行**没遵循原生格式** —— 链接文本是 `[mem]`(非描述标题)、摘要是 `score/kg://`(机器向,非明文)、路径多了 `memory/` 前缀。导致 CC 没法据明文摘要召回(违背"CC 能遵循 index"需求)。

**设计认知(推翻早期 ADR-19 草案)**:CC 召回是消解型(MEMORY.md 全量注入 + frontmatter description 代码层召回 + Read 工具),非 prompt 驱动。故 mem-service 不需要"hook 注入 context"路径 —— **只需产出原生相容的记忆文件 + 索引行,让 CC 自身召回机制捞**。project-back 方向正确,错在格式非原生。

---

## ADR-A:投影产物纯原生格式(执行)
Status: Accepted
Date: 2026-08-08
Context: 投影索引行/文件必须让 CC 原生召回机制(MEMORY 注入 + description 匹配)能消费,否则 KG fact 触达不到 agent。
Decision:
- MEMORY 索引行严格遵循原生 `- [Title](file.md) — hook` 结构,`mem-` 标识只放在文件名(不影响结构的位置)。指向方式与原生一致(相对路径)。
- 投影 .md 的 frontmatter `description` = 干净明文(= topic),不带 "mem-service KG fact, LIF X.XX" 机器噪声 —— CC 代码层召回靠 description 匹配。
- 投影 .md 正文自包含完整 fact 内容,不回查 KG(recall 时点的快照)。
Alternatives: (B) 独立命名空间文件 `.mem/*` —— 双机制并存增复杂度,否决。(C) hook 注入 context —— CC 无此必要(消解型召回),且单 session 反输 -17.7%,否决。
Consequences: recall 只建 .md(不碰 MEMORY);synthesis 扫 .md 写原生索引;CC 原生机能浮现。mem-service 不造注入路径。
Constrains: [T1]

## ADR-B:文件名严格契约 `mem-{4hex}-{slug}.md`(执行)
Status: Accepted
Date: 2026-08-08
Context: issue 1 的三法不一 + 创建侧不校验导致投影/native 混淆。需一个全仓共用的硬编码契约。
Decision:
- 文件名格式 `mem-{fact_id[:4]}-{sanitize(topic)}.md`(可读,非 32-hex)。
- 全仓唯一严格正则常量 `MEM_FILE_RE = ^mem-[0-9a-f]{4}-.+\.md$`,在**创建、识别、MEMORY 重写**三处共用(+ frontmatter `source: mem-service` 双闸)。
- `mem-` + 4hex + `-` 前缀天然区分 native(`mem-service-*` 的 `serv` 非 4-hex,不匹配)。
Alternatives: 32-hex 全哈希文件名(原设计)—— 对 CC 不可读,否决。松散 `startswith("mem-")` —— 误判 mem-service-*,否决。
Consequences: 修 issue 1 三法不一 + 创建侧不校验。fact_id 仍 uuid4-hex(前 4 位取短 id 保唯一)。
Constrains: [T1]

## ADR-C:EdgeOut 增 `topic` 字段,LLM 抽取时生成(执行)
Status: Accepted
Date: 2026-08-08
Context: 文件名 slug / 索引标题 / description 都需要一个干净可读的事实一句话。机械拼接 subject_predicate_object 不够好。
Decision: EdgeOut 增 `topic: str` 字段;两步 prompt 加第三元素 —— 每条 edge 附 LLM 生成的可读 topic。topic 流经 adapter._vote(透传)→ cli/autodream 消费 → projection 用作 slug 源 + 索引标题 + description。
Alternatives: recall 时再调 LLM 生成 topic —— 多一次调用 + recall 时 fact 已固定,不如抽取时一次性产出。
Consequences: P0 的 EdgeOut{subject,predicate,object} 增补为 {subject,predicate,object,topic}。prompt 两步 → 三元素。
Constrains: [T1]

---

## Deferred(记录方向,本迭代不执行)
以下为 KG 建图设计项(Pi 研究 R1/R2 判决),本迭代只记 ADR 方向,执行 defer 到后续迭代:

- **D3 实体解析(共指消解)**:新实体向量召回候选 → LLM dedupe 判定(抄 Graphiti dedupe_nodes),解 A2A/a2a 跨 ingest 分裂。→ 后续迭代(Pi 研究 R1 Tier 3)。
- **D4 双时态**:fact 带 valid_at/invalid_at(事实何时为真),新边进时检测同实体对矛盾 → 失效旧边。store.put_fact 的 valid_from/to 字段通电。→ 后续迭代(R2)。
- **D5 BFS 召回**:recall 加一路沿关系边 n-hop 找关联事实,与字面/向量三路 fusion —— KG 相对纯向量 RAG 的唯一独占价值。前提:entity↔entity 边存在(P0 已解)。→ 下一个迭代(P1)。
- **D6 gating**:recall 前判"近期上下文够不够",够则跳过,避 Graphiti 单 session -17.7% 反输。→ 随 D5。
- **D7 aliases 持久化**:EntityOut.aliases 存 store,find_entities_by_alias。→ 后续(R1 Tier 2)。

不抄(研究明确否决):GraphRAG hierarchical Leiden(coding-agent 非全局主题问答 + 全量重算不适合流式)、scale-free 幂律(营销,[弱证])。
