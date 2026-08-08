# Spec: bi-temporal-validity(迭代 4:D4 双时态有效性)

- **base**: `4f1c9f7`(bfs-recall-gating 之后, p0-entities-edges-schema 分支)
- **项目**: `/home/yy/projects/memory-service`(Python, 纯 stdlib+urllib+networkx)
- **背景**: fact 表【已有】`valid_from`/`valid_to` 列(schema.sql:27-28, put_fact store.py:168-169 已接参数), 但**未通电**: ① ingest 不填 valid_from(多 NULL);② autodream supersede(autodream.py:239 `update_fact_status(..., 'superseded', ...)`)只翻 status, **不设 valid_to**;③ recall 只过滤 status='active', 不查 valid_from/valid_to。研究 R2 L146-149/163: Graphiti bi-temporal = 矛盾失效(旧边 t_invalid=新边 t_valid)+ 点时召回;mem-service 现状是 T'(事件时态 status 机), 缺 T(有效时态区间)。**D4 = 把既有列通电 + 点时召回(复用既有 supersede, 不新增 LLM 矛盾检测)**。

## 已锁决策
- **列已存在, D4 = 通电非新增**: valid_from/valid_to 已在 schema。**不改 schema, 老库无需迁移**。
- **valid_from 默认 now**: put_fact 未传 valid_from 时 = 当前 ISO(`_now()`)。老数据 valid_from NULL 视为 **-∞(始终有效)**, 点时查询 `valid_from IS NULL OR valid_from <= t`。
- **supersede 设 valid_to**: `update_fact_status` 加 `valid_to` 参数;autodream.py:239 supersede 时传 `valid_to=_now()`(旧 fact 失效时刻)。status='superseded' 与 valid_to 同设(COALESCE 不覆盖已设)。
- **recall 默认 `valid_to IS NULL`**: 与 status='active' 一致(**零回归**, 防御性);NULL valid_from 视为 -∞(不加 valid_from 过滤)。
- **新能力 `--as-of`**: recall(`as_of=None`)点时召回;as_of 给定时候选过滤 `valid_from <= t AND (valid_to IS NULL OR valid_to > t)`(NULL valid_from 视为 <= t)。cli recall `--as-of <iso>` flag。**复用既有 supersede 触发器(autodream `_is_contradiction`), 不新增 LLM 矛盾检测**(Graphiti 式 defer)。

## Node A — 双时态通电(核心)
**改**:
- `store.put_fact`: valid_from 未传时默认 `_now()`(ISO)。确认 cli.ingest/autodream 不显式传 NULL 覆盖(让它走默认)。
- `store.update_fact_status`: 加 `valid_to: str | None = None` 参数;SQL 改 `UPDATE fact SET status=?, supersedes_id=COALESCE(?,supersedes_id), valid_to=COALESCE(?,valid_to) WHERE id=?`。`autodream.py:239` supersede 调用传 `valid_to=_now()`。
- `recall.py`: 默认过滤加 `AND valid_to IS NULL`(防御性, 与 status='active' 一致);统一应用到 `_facts_for_entities`(recall.py:167)、value_rows 查询(234/238)、`_build_centralities` 图构建(61)。NULL valid_from 不加过滤(视为 -∞)。
- `_decode_fact`: 确认 valid_from/valid_to 透出(decoder 已有 store.py:272-273, 确认无遗漏)。

**验收**: put_fact 默认 valid_from=now(非 NULL);supersede 后旧 fact valid_to 非空;recall 默认滤 valid_to IS NULL(零回归);现有全部 test 过 + 新 bi-temporal 测试过;db 隔离零污染。

## Node B — `--as-of` 点时召回 + 测试(依赖 A)
**改**:
- `recall()` 加 `as_of: str | None = None`;as_of 给定时, 候选过滤改 `valid_from <= as_of AND (valid_to IS NULL OR valid_to > as_of)`(NULL valid_from 视为 <= as_of)。应用到所有 fact 查询(_facts_for_entities / value_rows / _build_centralities)。BFS/vec 路径与 as_of 正交(都过滤候选集)。
- `cli.py`: recall 子命令加 `--as-of <iso>`(dest as_of);透传 recall_mod.recall。
- `test_bi_temporal.py`(新, db.init(tmp) 隔离):
  - 构 F1(subject=A, pred=uses, obj=rust, valid_from=t0);supersede(新 F2 不同 value, 触发 _is_contradiction)→ F1 status='superseded' + valid_to=t1。
  - 默认 recall(query 命中 A)→ F1 不在(valid_to 非空), F2 在。
  - `recall(as_of=t0 之前)` → F1 不在(valid_from=t0 > 早于 t0 的时刻), F2 不在。 / `recall(as_of=t0)` → F1 在, F2 视 valid_from。
  - `recall(as_of=t0 与 t1 之间)` → F1 在(valid_from=t0<=t, valid_to=t1>t), F2 在。
  - `recall(as_of=t1 之后)` → F1 不在(valid_to=t1<=as_of), F2 在。
  - 老数据 valid_from NULL → as_of 任意时刻都视为有效(-∞)。
  - 真断言副作用(fact_id 在/不在结果), 非假绿;db 隔离零污染。

**验收**: cli recall --as-of 端到端;点时召回时间区间对;default 零回归(现有 12 test 逐字过);bi-temporal 测试过。

## 约束
- **不破现有 recall**: 默认(valid_to IS NULL, 无 as_of)逐字零回归 —— 现有全部 test_*.py 过。
- 不破 P0+projection+entity-dedupe+bfs 契约(只加可选过滤 + 通电既有列, 不改实体/边/topic/BFS)。
- **不改 schema**(valid_from/valid_to 列已有); 不破 scoring weights 三元组。
- 测试隔离 db.init(tmp), 零污染 data/memory.db。
- **不 commit**(主会话做)。

## 后续(本 spec 不含)
Graphiti 式 LLM 矛盾检测(新 fact → LLM 比同实体对已存边 → 自动失效, R2 L148);bi-temporal churn 监控(supersede rate / active ratio, R2 L149/159);as_of + BFS 组合深测;valid_from 从 source_meta/会话时间推导(非 ingest now)。
