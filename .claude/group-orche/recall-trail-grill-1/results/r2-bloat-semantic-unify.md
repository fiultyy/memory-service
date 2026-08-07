# Round 2 终稿:形式1/2 投影源语义统一

## 【查证】
- `projection.py:69-93` update_memory_md:统一 mem-<id> 匹配,不区分来源。
- `cli.py:197-200` build_index:仅 top-K 查询,**无 --session 参数**;autodream 写 seen_sessions 但 build_index 不消费。
- `pre-compact-mem.sh:67-68`:调 build-index --scope $CWD,**未传 --session**。
- **当前只有形式2(top-K),形式1(轨迹)未实现**,两源合并语义不存在。

## 【结论】两源 UNION + 每次重写
- build_index 加 session_id 参数,UNION 查询(轨迹 seen_sessions LIKE ∪ top-K)。
- 清理:每次重写(全清 [mem] 段 → 重写本次投影)。
- 两源都动态重算 → 语义一致(都是"当下激活集")。重写后 MEMORY 开局 = 本次 session 召回 + 全局 top-K,历史轨迹自然消失(符合 ADR-16d)。
- 不分离清理(会语义分裂,违背 ADR-16c 一段合并)。

## 【修订方案】
ADR-16c 加 (g):build-index 加 --session 消费 seen_sessions(UNION top-K)。清理走每次重写(bloat-strategy-compare)。

⚠ **实现注意**:当前 MEMORY.md 无 `# [mem]` 段标题,update_memory_md 写的是散落 `- [mem]` 行(混 CC 原生行间)。"清空 [mem] 段"需先定义边界——用正则删所有 `[mem]` 前缀行 + 重写,或先聚集成段。P2 实现细节。
