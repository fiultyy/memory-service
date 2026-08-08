# Spec: entity-dedupe-aliases(迭代 2:D7 别名 + D3 两步实体合并)

- **base**: `3f56163`(projection-native-format 之后,p0-entities-edges-schema 分支)
- **项目**: `/home/yy/projects/memory-service`(Python,纯 stdlib+urllib,无框架)
- **背景**: P0 已修 schema(`{entities,edges}`,object 是实体,边出现);projection-native-format 已修投影原生格式 + EdgeOut.topic。本迭代治**孤儿实体根因**:跨 ingest 同实体不同写法(A2A/a2a/native agent/原生 agent)各建独立 entity。

## 已锁决策
- **合并策略 = 两步**:第 1 步廉价闸(`find_entity_exact` 大小写不敏感 + 别名精确),第 2 步向量召回 top-k + 大模型判定(Graphiti dedupe_nodes 风格)。
- provider 复用 `ZhipuAnthropicProvider`(llm_provider.py 已有,加 `dedupe_entity` 方法)。
- top-k 默认 5。
- aliases 存 `entity.aliases`(JSON TEXT 列)。
- few-shot 必含"相关但不同义**不合**"(Java 语言/Java 岛)、"同义异写合"(NYC/New York City)。

## Node A — D7 别名持久化(无依赖)
**改**:
- `schema.sql` + `db.init`: entity 加 `aliases`(JSON TEXT default '[]')+ `name_embedding`(JSON TEXT);老 db ALTER ADD。
- `store.put_entity(aliases=None, name_embedding=None)`: 存别名;embedding 不在 store 自动算(`name_embedding` 未传=空 `[]`, 由 resolver 算一次显式传入, skeptic 修正避污染 embeddings.db + 防火墙 block)。
- `store.find_entity_exact(name)`: 大小写不敏感**精确**匹配(name 或任一 alias 完全相等)→ entity|None。合并廉价闸专用(区别于 recall 的 LIKE 模糊查)。
- `cli._ensure_entity` / `autodream._resolve_subject`: 把 `EntityOut.aliases` 透传 `put_entity`。

**验收**: entity 表有 aliases+name_embedding 列;put_entity 存别名;embedding 不在 store 自动算(由 resolver 算一次显式传入, skeptic 修正避污染 embeddings.db + 防火墙 block);find_entity_exact 按别名精确命中;7 现有测试过 + 新别名测试过;db 隔离零污染。

## Node B — D3 两步合并(依赖 A)
**改**:
- `llm_provider`: LLMProvider Protocol + ZhipuAnthropicProvider 加 `dedupe_entity(new_name, new_type, candidates: list[{id,name,type,score}]) -> {duplicate_id: str|None}` + `_DEDUPE_PROMPT`(Graphiti few-shot + "NEVER mark related-but-distinct as duplicate")。
- `resolver.py`(新):
  - 第 1 步: `store.find_entity_exact(name)` → 命中返回既有 id + 并入新别名。
  - 第 2 步: `embedding.embed(name)` 余弦 top-k 候选(按 name_embedding)→ `provider.dedupe_entity` → 命中合并 / 未命中 `put_entity` 新建。
- `cli.ingest` / `autodream`: subject+object 改用 `resolver.resolve_entity`(替 `_ensure_entity`);合并后 fact 指向 resolved id。

**验收**: 两步逻辑对(廉价闸先截、难的才花大模型);同名异指不合 / 同义异写合;cli/autodream 真用 resolver;现有测试过 + resolver 测试过 + throwaway(同实体不同大小写 ingest 两次 → 库里 **1** entity 不是 2)。

## 约束
- 不破坏 P0 `{entities,edges}` 契约 + projection-native-format(文件名 MEM_FILE_RE / 原生索引 / topic)。
- 测试隔离(db.init tmp,不污染 data/memory.db)。
- 不 commit(主会话做)。

## 后续迭代(本 spec 不含)
迭代 3 = D5 BFS 召回 + D6 门控;迭代 4 = D4 双时态。
