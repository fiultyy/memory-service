# 运维: bi-temporal 时间语义

ADR-3 / D4 双时态通电后,`fact` 表的 `valid_from` / `valid_to` 是 bi-temporal 的核心列。
本文档记录运维必须知悉的时间语义,排障 / 迁移 / 查询时对照。

## 1. `valid_from` NULL = -∞

- **语义**: `valid_from IS NULL` 的行(老数据 / 迁移行 / D4 通电前的存量)视为 **valid_from = -∞**(从无穷早开始有效)。
- **影响**: 任何 `--as-of <t>` 点时召回都含这类行(只要 `valid_to` 未过),`_temporal_clause` 的
  `valid_from IS NULL OR valid_from <= ?` 子句显式放过。
- **运维动作**:
  - 不要手工把 NULL 补成 `_now()`——会把"从无穷早有效"误降为"从今天才有效",扭曲历史召回。
  - 若需统一非 NULL,只能补成该 fact 真实发生时间(通常无源,见 §3 defer)。
  - 生产 db 当前空(`data/memory.db`),存量风险暂为零;后续迁移若引入老行,务必保留 NULL 语义。

## 2. `_now()` 统一 UTC+00:00,秒级

- **语义**: 全代码库时间戳统一用 `datetime.now(timezone.utc)`,ISO-8601 带显式 `+00:00` 后缀,
  **精度截到秒**(`.replace(microsecond=0)`,与 `scoring.py` / `consolidate.py` 一致)。
- **为什么秒级**: ms-floor 惯例,避免 back-to-back `_now()` 调用微秒漂移在双时态半开区间
  (`valid_to = now` 与新 fact `valid_from = now`)上产生竞态。
- **存储格式**: SQLite TEXT 列存 ISO-8601 字符串(`2026-08-08T14:30:00+00:00`)。
  **必须全 UTC**——非 UTC 后缀会让 TEXT 字典序错序(见 §3)。
- **三处权威 `_now` 对齐点**: `store._now()` / `scoring.compute_lif` 内 `now` /
  `consolidate.consolidate` 内 `now`——任一处不一致都会引入 ms 漂移。

## 3. `--as-of` 输入归一(UTC)

- **问题**: `--as-of` 接受 ISO-8601,但 `Z` / `+08:00` / 无后缀三种写法混用会让 SQLite TEXT
  字典序比较错序(`2026-08-08T00:00:00Z` < `2026-08-08T00:00:00+00:00` 在 ASCII 上不真)。
- **归一**: cli 输入端解析任意 ISO-8601 → 转 UTC `+00:00` 再下传 `_temporal_clause`
  (ADR-3 ③,defer-cleanup 迭代 C1 实现)。**内部时间戳恒 UTC+00:00**,归一只发生在 cli 边界。
- **运维**: 手工 SQL 查 `valid_from` / `valid_to` 时,务必用 `+00:00` 后缀,勿混 `Z`。

## 4. deferred: `valid_from` 推导(非 ingest now)

- **现状**: `valid_from` 默认 ingest 时刻 `_now()`(摄入时间),非事实真实发生时间。
- **defer 理由**(ADR-3 ②, defer-cleanup 迭代 C2 探索结论):
  - `Extraction.source_meta` 仅含 `{provider, model, error}`,**无时间字段**——LLM 抽取的事实不携带可信发生时间。
  - `_read_transcript` 只拼 `message.content` 文本,丢弃 record 的 `timestamp`(那是会话事件时间 = 摄入近邻,非事实发生时间)。
  - **不强行造时间源**(ADR-3 Decision 明令): transcript timestamp 是 session 事件时间,不是事实发生时间,
    用它当 valid_from 会把"何时被发现"伪装成"何时为真",扭曲双时态语义。
- **升级路径**: 当有真实 source 带可信事实时间(如 git commit 时间 / 文档发布时间 / 用户显式标注),
  在 `autodream` 接收的 transcript / `source_meta` 加字段 → 透传 `put_fact(valid_from=...)`。
  无此源前,保持 ingest now。

## 参考

- ADR-3: bi-temporal 完善(`docs/specs/bi-temporal-validity.md` 衍生)
- D4 双时态通电: commit `60092a0`(`store.put_fact` valid_from 默认 / `update_fact_status` valid_to COALESCE / `_temporal_clause`)
- `recall.py:_temporal_clause` — NULL valid_from = -∞ 子句的实现点
