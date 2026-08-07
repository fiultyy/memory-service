# Grill 终稿:闭环未封污染面

## 【查证】逐路径核实(区分真污染 vs 幂等无害)
- **P1 init-memory**(`bootstrap.py:47`):真污染。glob("*.md") 无过滤 → 吃 source:mem-service 投影 md → 假 session `memory:mem-*.md#0` + fact_type 冲突 + 重复 ADD。**Node A T_A1 规划覆盖,代码未实施(P1 规划阶段)。**
- **P2 CC→KG 反向 re-ingest(defer)**:潜在污染。defer 项未实现;实现时须复用 bootstrap 过滤逻辑。
- **P3 autodream _read_transcript**(`autodream.py:37-73`):**非污染**。即使 session 内 agent Read 了 mem-*.md致 transcript 含其文本,autodream 抽出相同 (subject,pred,value) → exact match UPDATE 幂等,不重复 ADD。
- **P4 SessionStart build-index hook(defer)**:**非污染**。方向相反(KG→CC 投影),无 re-ingest。
- **P5 MEMORY.md [mem] 索引行**:**非污染**。索引行是 "- [mem] subject pred val(memory/mem-id.md) — LIF x.xx · kg://fact/id" 格式,LLM 难从中抽出干净 (subject,pred,value) 三元组,且即便抽出也 exact match 幂等。

## 【结论】未封污染面清单
- 当前真污染面:**仅 P1(init-memory)**,已被 Node A 覆盖。
- 未来潜在:**P2(CC→KG re-ingest defer)**,实现时加过滤。
- P3/P4/P5:非污染,无需封。

## 【修订建议】
1. Node A(bootstrap 过滤)即 ADR-16f 当前范围,**够用**(只封现有路径)。
2. P2 defer 实现时须加过滤(复用 bootstrap 的 source:mem-service 跳过逻辑)→ 在 defer 文档标注。
3. ADR-16f 无需扩范围。

**ADR-16f 闭环卫生:Node A 实施后即完全封堵当前路径;未来 re-ingest defer 须延续过滤。**
(注:teammate 关于"Node A/B/C 为何 pending"的尖锐问题基于流程误解——当前是 grill 阶段,尚未 go P2 执行,pending 正常。有效结论 = 污染面清单。)
