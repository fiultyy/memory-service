# Spec: defer-cleanup(13 项 defer 票清理)
Date: 2026-08-08
Status: Draft
Iteration base: `120b5ad`(branch p0-entities-edges-schema)

## §1 Problem
五迭代后 iteration log defer 总览列 14 项未完票(2 研究机制 + 12 minor 尾巴)。用户指定 ⚪ 全局 operational 10 项不碰,BFS_WEIGHT 调参因缺 eval 数据 defer → **本轮 13 项**。性质混杂(功能/健壮性/深测/文档),需编排实施 + ADR 约束。

## §2 Solution
按 5 个垂直切片节点实施(详见 ADR-[1..5] + §6 节点映射):
- R1 矛盾检测(纯 LLM 裁判)
- entity 健壮性(UNIQUE + GC + embedding 版本)
- bi-temporal 完善(_now 对齐 + as-of 归一 + NULL 文档)
- recall 增强(bfs hint + scoped opt-in)
- churn 监控(store stats + cli)

## §3 Out-of-Scope
- ⚪ 全局 operational 10 项(用户指定不碰)
- BFS_WEIGHT 调参(需 eval_recall grid,本轮无数据集)
- supersede_rate 降阈值自动刷新(监控先于自动化)
- daemon / 向量联邦 / 冷层归档(依赖未就绪)
- 反向 re-ingest / SessionStart hook(operational)

## §4 User Stories
- 作为 agent,ingest「X 位于 A」后再 ingest「X 位于 B」,旧 fact 自动 supersede(双时态失效),as_of 查询不重现矛盾 fact。
- 作为运维,`mem stats` 看 entity/fact 计数 + supersede rate,知 KG churn 健康度。
- 作为 agent,recall direct-match 薄时收到「可加 --bfs」提示,不静默丢图近召回。
- 作为开发者,embedding 模型升级维度变时,旧 name_embedding 被检测 + 惰性 re-embed,不静默失效。

## §5 Implementation Decisions (Constraints → ADR)
- 矛盾检测走 LLM: [ADR-1]
- entity UNIQUE/GC/embedding 版本: [ADR-2]
- bi-temporal _now/as-of/推导/文档: [ADR-3]
- recall hint/scoped: [ADR-4]
- churn store stats/cli: [ADR-5]

## §6 Acceptance(节点映射)
| 节点 | ticket | green 条件 |
|---|---|---|
| C bi-temporal 完善 | _now 对齐 / as-of 归一 / NULL 文档 / [valid_from 推导探索] / as_of+BFS 深测 | pytest 全绿(20+新增);ms-floor 不破坏字典序;--as-of 归一覆盖 Z/+00:00/无后缀 |
| A R1 矛盾检测(↣C) | judge_contradiction LLM / autodream supersede / 测试 | pytest 全绿;矛盾 supersede 设 valid_to;多值谓词 short-circuit 不误杀;provider 不可达 fallback 不阻断 |
| B entity 健壮性 | UNIQUE / aliases GC / embedding 版本双认 | pytest 全绿;put_entity 冲突 fallback;_cosine_topk 双认新老结构;库空迁移无负担 |
| D recall 增强(↣C) | bfs hint / scoped opt-in / BFS+use_vec 深测 | pytest 全绿;hint 不改 default;--bfs-scoped default off 零回归;组合深测覆盖 |
| E churn 监控 | store churn_stats / cli stats | pytest 全绿;stats 只读快照正确;cli 输出可读 |

## §7 Open Issues
(无 — MUST-ASK 已 Q1/Q2 收敛,余 AUTO 见 ADR)

## §8 Defer 预判(→ P4)
- BFS_WEIGHT 调参(eval 数据缺)
- valid_from 从 source 推导(source_meta 无时间字段,探索后大概率 defer)
- supersede_rate 降阈值自动刷新(监控先于自动化)
- ⚪ operational 10 项(用户指定)

## §9 Testing Decisions (Seams)
- seam: cli(argparse argv 同管道,ADR-1) — 所有 cli 新 subcommand(stats)/ flag(--bfs-scoped/--as-of 归一)走 argv 测试。
- seam: llm_provider Protocol(ADR-1) — judge_contradiction 用 fake provider mock 测试,不依赖真实 glm。
- 验证:qa_available=false → verify 全走 general_test(pytest 全绿)+ skeptic(每节点 ADR.Constrains acceptance),无 qa-intent。
