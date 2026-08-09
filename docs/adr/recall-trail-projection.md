# ADR: recall-trail-projection(召回轨迹投影 + 闭环卫生)
Date: 2026-08-07
Status: Active
Revision: 2026-08-07(post group-orche grill `recall-trail-grill-1`,2 轮 12 teammate 收敛;关键修订:env 取代 hook / 清空重写修 ADR-15 / score 阈值)
> **修订 2026-08-08(P3 清退)**:Decision 中的 `build_index`/`update_memory_md`/`[mem]` 索引行格式已在 P3 清退——`build_index`→`synthesis_index`、`update_memory_md`→`_rewrite_mem_lines`、索引行改原生格式 `- [{topic}](mem-{4hex}-{slug}.md) — {topic}`(无 `[mem]`/kg://,见 projection-native-format ADR-A);`recall.py:249` score≥0.3 过滤现 `recall.py:382`。决策逻辑(UNION 轨迹+top-K、env 取代 hook、清空重写、score 阈值)不变,仅命名/格式演进。


## ADR-16: 召回轨迹投影(recall-trail → MEMORY [mem])
Status: Accepted
Date: 2026-08-07

Context: ADR-15 分布式 index 只有 `build_index` 一条 KG→CC 投影通路(全局 LIF top-K)。"本次 session 实际召回过的 fact"无投影通路(形式1缺失);且 `bootstrap` 不过滤投影 md 导致 re-ingest 污染(ADR-15 既有 bug)。

Decision(grill 收敛后修订):
- (a) **轨迹载体 = `fact.seen_sessions` + `CLAUDE_CODE_SESSION_ID` env 自动读取**(取代原 --session 强制/hook/文件方案)。CC 内建注入该 env 到所有 tool 子进程(主 session Bash tool + teammate + hook,**实测验证** `=418e378b-...` 主 session 与 teammate 同源)。`recall()` session_id 默认 `os.environ.get("CLAUDE_CODE_SESSION_ID","unknown")`;保留可选 `--session` 覆盖。**无需 UserPromptSubmit hook、无需 .last_session_id 文件**。
- (b) `build_index` session_id 从 env(或参数),**UNION 查询**:轨迹(`seen_sessions LIKE %sid% + source_cwd=cwd`)+ LIF top-K。
- (c) **注入形态:一段 `[mem]` 合并去重**;**清理:每次 build_index 清空重写 [mem] 段**——改 `update_memory_md` 用正则删所有 `[mem]` 前缀行 + 重写本次投影集(当前 MEMORY.md 无 `# [mem]` 段标题,投影是散行,故用正则而非段边界)。**修 ADR-15 既有累积 bug**(grep 确认 update_memory_md 仅 build_index 调用,改它 = 修 bug 非违背)。
- (g)(新增)`build_index` 加 `session_id` 参数消费 seen_sessions(UNION 两源)。
- (噪音,新增)`recall.py:249` `score >= 0.3` 过滤(ADR-4v2 α=0.5 对齐,match≥0.6 达标);cli 加 `--top-k`(默认 None 保留兼容)。投影层自动受益,低相关尾巴不灌 CC 热层。
- (d) 仅本次 session;(e) NULL source_cwd 严格不投影;(f) bootstrap 过滤 source:mem-service md —— 不变。

Consequences(修订):
- **recall 闭环**:env 让 seen_sessions 可靠累积 → 形式1 有数据;投影 md 带 `kg://fact/<id>` 反查;bootstrap 过滤保不被吃回污染。
- **形式1 边际价值**(grill 量化):首次召回/长期未访问/cwd 专属 fact 的 LIF 窗口期(0.60 vs 老 fact 0.77+,gap 17%+),形式1 唯一覆盖,保留;score 阈值已解噪音。
- **清空重写**:`[mem]` 段每次 = 本次投影精确集合,无累积膨胀(修 ADR-15);MEMORY 开局只见"当下激活集"(符合 ADR-16d)。
- 测试隔离:`db.init(tmp)`;env 测试用 `CLAUDE_CODE_SESSION_ID` 注入。

Alternatives(grill 证据,均否):
- UserPromptSubmit hook:原脚本 LLM 调用/validate 阻断风险,2026-03 统一禁用;env 零侵入更优(chain-hook-feasibility)。
- .last_session_id 文件:多 session 并发覆盖无解,CLI 不知当前 session,循环依赖(chain-concurrency)。
- transcript 解析 tool_result:autodream 跳过 tool_result,脆弱。
- 两段 [mem-recall]/[mem-lif]:增复杂;一段合并 + 清空重写已统一两源。
- LIF 阈值淘汰/滑窗:违 ADR-16d 语义 / 需状态管理;清空重写更简(bloat-strategy-compare)。

Constrains: [T_A1, T_B1, T_C1, T_C2, T_C3, T_C4]
