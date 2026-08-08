# ADR: defer-cleanup(13 项 defer 票清理)
Date: 2026-08-08
Status: Active
Iteration base: `120b5ad`(branch p0-entities-edges-schema)

承接五迭代(P0 边 / projection / dedupe / bfs / bi-temporal)后, iteration log defer 总览中 14 项未完票的清理轮。⚪ 全局 operational 10 项用户指定不碰(留 defer)。BFS_WEIGHT 调参因缺 eval_recall grid 数据集 defer。**本轮 13 项**。

---

## ADR-1: R1 矛盾检测 = 纯 LLM 裁判(Graphiti 式)
Status: Accepted
Date: 2026-08-08
Context: 当前 `autodream._is_contradiction` 是硬编码 functional predicate 集 `{is_a,belongs_to}` + 纯值比较,覆盖不了开放式谓词(中文同义异写矛盾如「X 位于 A」vs「X 位于 B」)。Graphiti R2 L148 式矛盾检测是 KG 双时态的另一半:新 fact → 比同实体对已存边 → 自动失效。用户选纯 LLM(非 hardcode 快路径)。
Decision:
- `llm_provider.Protocol` 加 `judge_contradiction(subject, predicate, new_value, old_value) -> {contradiction: bool, reason: str}` 方法;`ZhipuAnthropicProvider` 实现(`_CONTRADICTION_PROMPT` few-shot,类 `dedupe_entity`:明确「多值谓词(uses/depends_on)共存不矛盾,单值属性(is_a/located_in)新旧值不同才矛盾」,NEVER 误判 related-but-distinct)。
- `autodream` 新 fact ingest 时,对同 `subject_id+predicate` 的已存 active fact(`_has_active_for_predicate` 已有)调 `judge_contradiction`;矛盾 → supersede 旧 fact(`update_fact_status(..., "superseded", supersedes_id=new, valid_to=now)`,复用既有 bi-temporal supersede)。
- 多值谓词 short-circuit:已知多值集 `{uses,depends_on,contains,implements,connected_to,part_of,relates_to}` 直接判 no-contradiction,不走 LLM(省调用,防 LLM 误判共存)。
Alternatives: (a) hardcode 快路径 + LLM fallback(未选,用户要纯 LLM 最贴近 Graphiti);(b) 仅扩 hardcode 谓词集(未选,覆盖不了开放式)。多值 short-circuit 保留为防 LLM 误判的成本优化,不违背「单值矛盾走 LLM」。
Consequences: 每次 ingest 同 subject-predicate 对多一次 LLM 调用(延迟/成本);依赖 provider 可达(不可达 fallback = 不判矛盾,记 source_meta.error,不阻断 ingest)。supersede 设 valid_to 与 bi-temporal 一致。
Constrains: [A]

## ADR-2: entity 表健壮性 = UNIQUE 约束 + aliases GC + embedding 版本
Status: Accepted
Date: 2026-08-08
Context: entity 表三处审查盲区:① 无 `UNIQUE(name,entity_type)`,并发 re-ingest 同实体两 INSERT 竞态建孤儿(resolver 是应用层两步闸,非 DB 强制);② `add_aliases` 只加不删无 GC,合并后旧实体别名残留;③ `name_embedding` 纯 JSON 向量无 model/dim 标记,模型升级维度变靠 `len` 过滤(脆,无迁移信号)。
Decision:
- schema.sql entity 表加 `UNIQUE(name, entity_type)`;`db.init` 迁移用 `ALTER ADD CONSTRAINT` 失败则 IGNORE(老库已有冲突行先 consolidate dedup);`store.put_entity` 捕 `IntegrityError` → fallback `find_entity_exact` 复用既有行(与 resolver 一致语义)。
- `store` 加 `set_aliases(entity_id, aliases)`(全量替换)+ `remove_aliases(entity_id, to_remove)`;resolver 合并 survivor 时,被合并实体的 name 若已是 survivor 别名则不再重复,清理无效别名。
- `name_embedding` JSON 改 `{"v":[...],"model":"...","dim":N}` 结构;`resolver._cosine_topk` 与 `backfill_entity_embedding` 兼容新结构,**双认老结构**(裸 list / `'[]'` / NULL)→ 读时检测,老结构惰性 re-embed 升级。
Alternatives: (a) UNIQUE 用应用层保证不碰 schema(未选,DB 强制更稳,库空无迁移负担);(b) embedding 加 `embedding_dim INTEGER` 列(未选,JSON 嵌 metadata 单点改动不改 schema 列);(c) aliases 不加 GC(未选,残留别名污染召回)。
Consequences: UNIQUE 约束使 put_entity 在冲突时走 fallback(多一次查询);embedding 双认增加 _cosine_topk 复杂度(过渡期,全库 re-embed 后可清理老路径);生产 db 当前空,迁移零负担。
Constrains: [B]

## ADR-3: bi-temporal 完善 = _now 对齐 + valid_from 推导 + as-of 归一 + NULL 文档
Status: Accepted
Date: 2026-08-08
Context: bi-temporal 四处 minor:① `store._now()` 无 ms-floor,与 `scoring.py`/`consolidate.py` 的 `.replace(microsecond=0)` 不一致,三处 _now 语义不一;② `valid_from` 默认 ingest now,非事实发生时间(理想应从 source/会话推);③ `--as-of` 非 UTC 输入会字典序错序(SQLite TEXT 比较);④ 老数据 valid_from NULL=-∞ 需运维知悉。
Decision:
- `store._now()` 加 `.replace(microsecond=0)` 对齐 ms-floor 惯例(三处统一)。
- `valid_from` 推导:**探索性** — autodream 接收 session transcript 时,若 transcript 含时间戳则传 `valid_from`(事实发生时间)给 put_fact;若 source_meta 无时间字段(现状),则本轮简化为 defer(保持 ingest now),记 P4。不强行造时间源。
- cli `--as-of` 输入端归一:解析任意 ISO-8601(含 `Z`/`+08:00`/无后缀)→ 转 UTC `+00:00` 再下传 `_temporal_clause`,杜绝非 UTC 字典序错序。
- `valid_from NULL=-∞` 语义写入 docs/(运维知悉:老行 valid_from NULL 视为 -∞,as_of 查询含全区间)。
Alternatives: (a) valid_from/valid_to 改 REAL Unix epoch(未选,改 schema 类型 blast radius 大,TEXT ISO-8601 + 格式统一够用);(b) _now 不对齐(未选,三处不一 是隐患)。
Consequences: ms-floor 使时戳精度降到秒(可接受,与 scoring/consolidate 一致);--as-of 归一加解析开销( negligible);valid_from 推导大概率 defer(无时间源)。
Constrains: [C]

## ADR-4: recall 增强 = bfs hint + 跨 cwd opt-in scoped
Status: Accepted
Date: 2026-08-08
Context: recall 两处 minor:① BFS opt-in(`--bfs`)default off,direct-match 薄时用户不知可 rerun --bfs 捞图近 fact;② BFS 在全局图跑(图构建不受 source_cwd 影响),跨 cwd 共享是 ADR-14 单体 KG 设计意图,但缺 opt-in scoped 选项。
Decision:
- `recall` direct-match 薄(候选 < 阈值)且 use_bfs=False 时,返回 `suggest_bfs=True` 提示;cli 显示「💡 可加 --bfs 扩展图近召回」。
- 跨 cwd BFS 门控 = opt-in `--bfs-scoped` flag(default off 保持全局图,契合 ADR-14 单体 KG 跨 cwd 共享);on 时 `_build_entity_graph` 加 cwd 过滤(图更精确但更小)。
- BFS+use_vec 组合深测:加测试覆盖 use_bfs=True + --vector 组合(图近 + 语义近双路候选)。
Alternatives: (a) BFS 默认 scoped(未选,违背 ADR-14 跨 cwd 共享设计);(b) 不加 hint(未选,direct-match 薄时静默丢召回)。
Consequences: hint 不改 default recall 行为(只加提示字段);scoped flag default off 零回归;两个 opt-in flag(`--bfs`/`--bfs-scoped`)组合状态空间增,需测试覆盖。
Constrains: [D]

## ADR-5: churn 监控 = store 统计 + cli stats
Status: Accepted
Date: 2026-08-08
Context: bi-temporal churn(supersede rate / active ratio)无监控,R2 L149/159 要求降阈值触发刷新。`consolidate` 已返 `{decayed,deprecated,superseded,active}` 单次快照,非可观测指标。
Decision:
- `store` 加 `churn_stats() -> {active, deprecated, superseded, supersede_rate, active_ratio}`(基于 status + created_at + valid_to 现有列,纯 SQL 聚合,无新列)。
- cli 加 `stats` subcommand 展示 churn_stats + entity/fact 计数(复用 `count_entities`)。
Alternatives: (a) 直接 sqlite3 SQL 脚本零代码(未选,cli subcommand 更可发现);(b) consolidate 返回值扩展(未选,consolidate 是写操作,stats 是只读查询,职责分离)。
Consequences: stats 是只读快照(非时间序列,历史 churn 需另搭日志);supersede_rate = superseded/(active+superseded) 简单比率,降阈值触发刷新逻辑本轮不加(监控先于自动化)。
Constrains: [E]

---

## defer 预判(本轮不碰,记 P4)
- **BFS_WEIGHT 调参** — 需 eval_recall grid(ADR-9 baseline)数据驱动,本轮无数据集 → defer。
- **valid_from 从 source 推导** — source_meta 无时间字段,本轮探查后大概率 defer(保持 ingest now)。
- **⚪ 全局 operational 10 项** — 用户指定不碰(autoDream daemon / 中文 embedding 调优 / on-ingest 预计算 / 向量联邦 / 增量检测 / 新 provider / 冷层归档 / 反向 re-ingest / SessionStart hook / daemon)。
- **supersede_rate 降阈值自动刷新** — churn 监控(stats)先于自动化,本轮只暴露指标。
