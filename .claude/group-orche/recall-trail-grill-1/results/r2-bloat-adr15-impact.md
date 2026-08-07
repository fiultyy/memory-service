# Round 2 终稿:波及 ADR-15 既有 top-K

## 【查证】
- 调用方:grep 确认 update_memory_md / project_fact_md **仅被 cli.build_index 调用**,无其他调用方。
- 路径:projection.py:69-103 / cli.py:183-208 / hooks/pre-compact-mem.sh:21。
- ADR-15:语义是"top-K 快照投影",**累积是历史 bug(非设计意图)**。

## 【结论】
1. 波及面:仅 build_index 路径,无其他调用方。
2. 语义对齐:改 update_memory_md 为"清空重写"= **修正 ADR-15 实现**(非违背 ADR-15,是修 bug)。
3. 不拆分投影路径:单一投影点更简。

## 【修订方案】
统一改 update_memory_md 为"清空重写 top-K 快照"(修复累积 bug)。编排图:**Node C 扩容**(加清理 task),无需新 Node。边标注"重写(清旧行)"。
