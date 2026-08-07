# Round 2 终稿:清理策略权衡

## 【结论】推荐:**清空重写**
- 复杂度:低(~15 行)
- 语义:MEMORY [mem] 段仅反映本次 session 投影(ADR-16d 完全契合)
- 成本:0 新参数 / 0 状态 / 仅改 update_memory_md
- 用户看到的 = 最近一次 compact 结果,清晰

## 舍弃
- **LIF 阈值淘汰**:违背 ADR-16d(保留历史旧 fact,混淆"本次"语义)
- **滑窗**:需状态管理(compact 计数器),复杂且跨 session 累积

## 【修订方案】改 projection.py:69-93 update_memory_md
1. 检测 [mem] 段边界(`# [mem]` 标题)
2. 保留 [mem] 段之前的 CC 原生行
3. **清空 [mem] 段内容**,重写本次 top-K
4. 删 :87 `out.append(line)` 非匹配保留逻辑

```python
# Phase 1: 保留 [mem] 段之前的行 + [mem] 标题
for line in lines:
    if line.startswith("# [mem]"): in_mem=True; out.append(line); continue
    if in_mem and line.startswith("#"): in_mem=False  # 新 section
    if not in_mem: out.append(line)
# Phase 2: 重写 [mem] 段(本次投影集)
for f in facts: out.append(mem_index_line(f, subj))
```

**无需改 build_index/cli.py,逻辑内聚。**
