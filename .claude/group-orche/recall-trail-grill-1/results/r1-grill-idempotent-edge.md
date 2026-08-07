# Grill 终稿:幂等边界 + MEMORY.md 膨胀

## 【查证】
1. **update_memory_md 非匹配行保留**:`projection.py:79-85` 正则匹配 mem-<id>.md 行 → 在本次 facts 中则 update(LIF 变)入 seen;**`projection.py:86-87` else: out.append(line) 非匹配行完全保留**;`projection.py:88-91` 仅追加 not in seen 的新 fact。
2. **build_index 不清理旧行**:否。`cli.py:197-200` SQL LIF DESC LIMIT top_k(默认 20),但 projection.build_index 只投影本次 facts,update_memory_md 不删不在本次 facts 的旧行。
3. **top-K 行数限定**:单次有界(SQL LIMIT 硬截断);累积无界(MEMORY [mem] 段总行 = 本次 top-K + 所有历史未清理旧行)。
4. **重入幂等**:有界。build_index --session 查询条件固定(seen_sessions LIKE + source_cwd=cwd),同 session 重跑相同 fact 集;seen set 保证重复 id 是 update 非追加。

## 【结论】无界膨胀
上界:最坏 N compact × M 投影 fact = N×M 行(线性累积)。每 session 轨迹行留下,LIF 跌出 top-K 的旧行也保留。**无任何截断/淘汰**。

## 【修订建议】
- **选项1(推荐)**:每次 build_index 先清空 [mem] 段重写。改 update_memory_md:检测 [mem] 段标记 → 清空后续 → 重写本次投影。最简单,保证 [mem] 段 = 本次投影精确集合,符合 ADR-16d "仅本次 session" 语义。ADR-15 全局 top-K 本就与 recall-trail 投影源语义冲突。
- 选项2:LIF 阈值淘汰(加 --lif-threshold,跳过 LIF<阈值的旧行)。
- 选项3(不推荐):标 defer。若 defer,ADR-16 Consequences 必须加"已知限制:MEMORY.md [mem] 段无界膨胀,待 ADR-XX 清理策略"。

**裁决:选项1 最符合 ADR-16 设计;不改则 defer 须在 ADR-16 明确标注已知限制。**
