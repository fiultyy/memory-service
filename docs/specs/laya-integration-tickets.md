# Laya 集成实施 Tickets(P0–P5.5)

- 依据: `docs/specs/laya-integration-spec-v1.1.md`(2026-09-29)
- 通用规则(每票生效): 主干零改动红线见 spec §五;回退=env 开关(`MEM_LAYA_ENABLED=0` 全链路=现状);准出=全量 pytest(484+)不降。
- 依赖图: **P0 → {P1, P2, P3, P4, P5} → P5.5**(P5.5 另需谓词表定稿)。每票独立可测可合。

---

## T0 · P0 地基: laya_client.py

**目标**: 统一批量原语,一切 Laya 调用的唯一出口。

### 实施步骤
1. 新建 `laya_client.py`:
   - `LAYA_BASE = os.environ.get("MEM_LAYA_URL", "http://127.0.0.1:8190")`——base URL 无 path(v1.0 `/predict/health` 拼接 bug 禁止复发);
   - `laya_available() -> bool`: `GET {LAYA_BASE}/health`,**进程级 TTL 缓存 60s**(成功/失败都缓存),`MEM_LAYA_ENABLED≠1` 时短路 False 零网络;
   - `laya_batch(state, questions, timeout=30.0) -> dict | None`: 单次 POST `/predict`;token 预算 8000(`len(state)//4 + Σlen(json(q))//4`),超预算 → question 均分 N 片、state 原样复制;**任何失败(HTTP/超时/answers 键缺失)→ None,不逐 question 重试**;
   - `norm_score(answer, n_criteria) -> float = answer["score"] / (n_criteria - 1)`: 全集成唯一归一出口。
2. `.env.example` 增: `MEM_LAYA_URL` / `MEM_LAYA_ENABLED`(缺省 0)/ `MEM_LAYA_FILTER`。
3. 新建 `test_laya_client.py`(锚见验收)。

### 验收要求
- [ ] `norm_score`: 3 档 score=2.0 → 1.0;4 档 3.0 → 1.0;0.0 → 0.0(两档口径单点)。
- [ ] `laya_batch` mock: answers 原样透传;构造 >8000 token questions → 断言分片 ≥2 次调用且每片携带同 state。
- [ ] 整批失败矩阵(mock 500 / 超时 / 返回无 `answers` 键)→ 一律 `None`,无异常外抛。
- [ ] `laya_available`: 首探后 TTL 窗口内第二次调用零网络请求(mock 计数);探测失败缓存 False 不抛;`MEM_LAYA_ENABLED=0` → False 且零网络。
- [ ] `pytest test_laya_client.py` 全过;全量 484 不降(新文件零侵扰)。

**回退**: 删两文件即可,无任何依赖方。

---

## T1 · P1 Gate 批量(spec ③ 落地,交互路径)

**目标**: B 翼 gate 的 LLM provider 位换 Laya 单次批量,延迟 <1s。

### 实施步骤
1. `gate.py` 新增 `run_gate_laya(cand_texts: dict[str,str], query: str, anchors: set[str]) -> dict[str, dict] | None`:
   - state = `Query: {query}` + 每候选一行 `[fid] {text}`;questions = 每候选 1 个 score(3 档 low/medium/high),instructions `Relevance of [{fid}] to the query?`;
   - `keep = probabilities["2"] >= 0.35`;`match_score = norm_score(a, 3)`;
   - matched_anchor 程序化: `anchors` 子串匹配候选文本(大小写不敏感),keep 但锚不上 → `keep=False`;命中 → verdict 带 `matched_anchor`;
   - `laya_batch` 返回 None → 整体 None。
2. `recall.py` gate 调用点(:479-484)二选一: `laya_available()` → `run_gate_laya`(anchors = A 路命中 fact 的 subject name+alias 集合,现场收集);结果 None → 回落原 `gate.run_gate`。verdicts 消费/入账/`verdicts is None → B翼全不入`分支**零改动**。
3. 新建 `test_laya_gate.py`。

### 验收要求
- [ ] mock `laya_batch`: 恰 **1 次调用**、questions 数 == 候选数、state 含全部候选行。
- [ ] 阈值两侧: P(high)=0.34 → 丢弃;0.36 → keep。
- [ ] `match_score == score/2` 且域 [0,1];`bump_gate_score` 收到该值(gate_account=True 时 mock 断言)。
- [ ] anchor: keep 候选无 anchor 子串 → keep 翻 False;有 → `matched_anchor` = 命中子串。
- [ ] 整批 None → B 翼全不入、A 路全保留(现有降级语义锚)。
- [ ] `MEM_LAYA_ENABLED=0` → 原路径: 现有 gate 相关测试(bfs_recall 等)逐位不降。
- [ ] **真实服务实测**: 8 候选端到端 <1s,记录数值。
- [ ] 50 对标注集准确率 >80%(前置: 标注集先建,可本票并行产出)。

**回退**: env 关 → 原 run_gate。

---

## T2 · P2 矛盾裁决(组合降级)

**目标**: `providers[0].judge_contradiction` 位换 Laya,`_judge_contradiction` 主干零改动。

### 实施步骤
1. `llm_provider.py` 新增 `LayaJudgeProvider`:
   - `__init__(self, fallback: LLMProvider | None = None)`;
   - `judge_contradiction(subject_type, subject_name, predicate, new_value, old_value)`: state = old/new 三元组对照行,1 noul "Should old be superseded by new?";`>=0.5` → `{"contradiction": True}`;
   - Laya 不可用/返回 None → fallback 非 None 则 `fallback.judge_contradiction(...)` 原样委托;fallback 为 None 或也抛 → `raise ProviderCallError`(外层现有 except → False)。
2. `cli.py` autodream/ingest 子命令 providers 构造位: 开关开且 available → `providers.insert(0, LayaJudgeProvider(fallback=原 providers[0] if providers else None))`。
3. 新建 `test_laya_judge.py`。

### 验收要求
- [ ] noul 0.5/0.49 两侧 → True/False(返回形状 `{"contradiction": bool}`)。
- [ ] 降级矩阵: Laya 挂 → fallback.judge_contradiction 被调(mock 断言透传参数);双挂 → ProviderCallError;外层 `_judge_contradiction` 捕获 → False(supersede 不发生)。
- [ ] multivalue 谓词/同值 → **零网络**(快路径在 judge 前,mock laya_batch 断言未调)。
- [ ] 现有 `test_autodream_supersede` / `test_autodream_txn_scope` 不降。
- [ ] 与 LLM 裁决 100 对一致率 >85%(评测脚本一次性,产物存 temp/)。

**回退**: env 关 → 原 providers 列表。

---

## T3 · P3 提取前过滤(E1)

**目标**: 噪声段不烧 LLM 调用;召回 100% 保底。

### 实施步骤
1. `autodream.py` `_decide_segments`: `MEM_LAYA_FILTER=1 且 laya_available()` → 全 segments 一次 `laya_batch`(每段 1 noul "Does [seg i] contain extractable facts?");`noul < 0.4` 的段跳过 LLM 提取通道(**regex/gazetteer 通道照跑**);批 None → 全不过滤。
2. 新建 `test_laya_filter.py`。

### 验收要求
- [ ] mock: 低分段不进 LLM 提取、高分段照常;**regex 通道对所有段不跳**。
- [ ] 整批 None → 行为与开关关逐位一致。
- [ ] 现有 autodream 测试不降。
- [ ] 实测口径: 真实 corpus 抽样 ≥200 段,记录过滤率(供 spec §六 ">30%" 验收;不达阈值不阻塞合入,记录 OQ)。

**回退**: `MEM_LAYA_FILTER` 关。

---

## T4 · P4 语义边 + fact_relations schema

**目标**: B 路图获得语义级边;生命周期与 fact 软删一致。

### 实施步骤
1. `db.py` + `schema.sql`: `fact_relations(source_id, target_id, edge_type DEFAULT 'semantic', weight, created_by DEFAULT 'laya', created_at, PRIMARY KEY(source_id, target_id, edge_type))` + `idx_fact_relations_target`;照 upgrade_queue 幂等先例。
2. `store.py`: `put_semantic_edges(edges)`(INSERT OR REPLACE)/ `get_semantic_edges(as_of=None, source_cwd=None)`——**读侧时态联查**: JOIN fact 两端 `status='active' AND valid_to IS NULL`(+source_cwd 过滤,与 `_build_entity_graph` 同构)。
3. 新建 `semantic_edges.py`: `discover_semantic_edges(new_facts, neighbors)`: 每 fact 一次 `laya_batch`(每邻居 1 个 4 档 score q);`norm_score(a,4) >= 0.7` 入边;批 None → 该 fact 跳过。neighbors = 新 fact subject 的 1-hop 邻域 active fact(`_facts_for_entities` 同源查询)。
4. `recall.py` `_build_entity_graph`: `get_semantic_edges()` 并入 nx.Graph(带 weight 属性);BFS/centrality/M3 零改动。
5. `autodream.py` ingest 成功收尾: 调 discover → put(开关同 `MEM_LAYA_ENABLED`)。
6. 新建 `test_laya_semantic_edges.py`。

### 验收要求
- [ ] schema: 老库连续两次 init 幂等无错。
- [ ] put: 同键重写 → weight 更新不重复行。
- [ ] get 时态: 任一端 `valid_to` 置值 → 边消失;`as_of` 时间窗正确(复用 test_bi_temporal 手法)。
- [ ] 图: 造无共享实体的两条 fact + 一条语义边 → BFS 可达(hop=1);**无语义边时 BFS 行为与基线逐位一致**(test_bfs_recall 不降)。
- [ ] mock: 每 fact 恰 1 次 laya_batch,questions 数 == 邻居数,4 档归一 `/3.0`;批 None → 零边不炸。
- [ ] 延迟实测: 10 fact × 20 邻居 ≤ 60s(daily 窗口预算)。

**回退**: env 关(边发现停);表与存量边留存无害(读侧仍正确联查)。

---

## T5 · P5 实体消歧(resolver step2 裁判位)

**目标**: "同一实体?"裁判换 Laya choice;召回管道与 step1/step3 不动。

### 实施步骤
1. `llm_provider.py` `LayaJudgeProvider` 补 `dedupe_entity(new_name, new_type, candidates, context=None)`:
   - state = 待判名 + **context 原文片段**(必带,裸名实测 confidence 0.06)+ 候选清单行;questions = 1 choice(`criteria={canonical_name: "canonical entity: {name} ({type})"}`);
   - `confidence >= 0.5` → 返回命中 canonical;否则 None(不合并);不可达 → fallback 委托(同 T2 模式)。
2. `cli.py` 注入位已在 T2 完成(同一 provider 对象),本票零额外接线。
3. 新建 `test_laya_dedupe.py`。

### 验收要求
- [ ] choice 命中 → resolver 返回既有实体 id 且 alias 并入(mock 断言 store 层调用);`confidence 0.49` → None → resolver 走 step3 新建。
- [ ] 降级: Laya 挂 → fallback.dedupe_entity 透传(含 context 参数)。
- [ ] 现有 `test_aliases.py` / `test_aliases_gc_embedding.py` 不降。
- [ ] **真实服务案例**: 带 context 的 "memsvc→memory-service" confidence ≥0.5 记录实测值;<0.5 则记 OQ(阈值可调,不阻塞)。

**回退**: env 关 → 原 LLM 裁判。

---

## T5.5 · P5.5 E3 谓词归一(可选票,依赖谓词表定稿)

**目标**: 谓词异写在**写入时**归一到封闭词表;夜间判等继续吃干净键。

### 实施步骤
1. 产出封闭谓词表: 从现库 `SELECT predicate, COUNT(*) FROM fact GROUP BY predicate` 提取 top 高频 + 人工审核语义档(目标 ≤50 词),表文件入库(docs/specs/ 或常量表)。
2. `autodream.py` 提取产出后、入库前挂点: 新谓词(不在表内)→ 1 choice "Which canonical predicate?"(criteria=词表);`confidence >= 0.5` → 替换;否则**保留原词不归一**。批量: 多 fact 一次批(每 fact 1 q)。
3. value 槽确定性 normalize 同票落地: `lower().strip()` + 连字符/下划线变体归一(纯规则,写 `store.put_fact` 入库前)。
4. 新建 `test_laya_prednorm.py`。

### 验收要求
- [ ] 词表映射抽检 20 例准确(utilizes→uses 等);词表外/低置信 → 原词保留(零误归一锚)。
- [ ] value normalize: "SQLite-vec"→"sqlite-vec"、"PGVector "→"pgvector";中文 value 不被误伤(仅空白/大小写归一,不改内容)。
- [ ] 归一后同键 fact 夜间 dedup 命中(端到端: 异写两条 → consolidate 后合 1 条)。
- [ ] 现有全量测试不降。

**回退**: env 关;存量数据不回改(归一仅写入时)。

---

## 全局准出(所有票合入后)

- [ ] 全量 pytest(484 + 新增)不降;
- [ ] `MEM_LAYA_ENABLED=0` 冒烟: recall/ingest/consolidate 输出与基线逐位一致;
- [ ] 真实服务延迟记录: Gate <1s / daily 窗总增 <60s;
- [ ] spec §六 验收表逐行核销(标注集/一致率/过滤率/边抽检)。
