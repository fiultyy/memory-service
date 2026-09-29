# Laya × memory-service 集成方案 v1.1
# 2026-09-29 · 基于 v1.0 + grill v1(temp/laya-integration-grill-report-v1.md)
#            + 批请求实测(temp/laya-batch-request-design.md)+ D1 裁决(2026-09-29)
# 原则: System 1 (Laya) 做判断/门控/评分, System 2 (LLM) 做提取/生成
# 核心红线: 语义判断只放"拒绝是安全默认"的位置(写入时判不准→不归一/不合并);
#           夜间合并层(consolidate)维持纯哈希判等, 永不 Laya 化

---

## 0. Laya 批量契约(2026-09-29 实测, 替代 v1.0 的 42ms 单点推算)

| 场景 | 规模 | 实测延迟 |
|---|---|---|
| 单 question 小 state | 1q | 37-54ms |
| Gate 全批 | 8 候选×2 问 = 16q | 712ms |
| 语义边单 fact | 20 score q | 1531ms |
| 上限探测 | 100q | 8162ms(无上限, 100 answers 全返回) |
| 大 state | ~7.2k token + 8q | 1476ms |

- **延迟 ≈ f(questions × state)**, 非恒定; 增量 ~50-80ms/q; questions 共享一次 state 编码。
- **score 返回期望值**(连续域): `score = Σ i×P(i)`。3 档值域 [0,2]、4 档 [0,3]。
- 归一单点: `norm_score(a, n) = a.score / (n-1)` —— 3 档 /2.0, 4 档 /3.0(v1.0 §一 `/3.0` 对 3 档系统性压缩 1/3, 破坏 N2 解锁账本, 已修正)。

---

## 总架构

```
用户消息 → PreCompact hook (实时, spool 异步)
              transcript 入 spool → spool-worker 异步 ingest

ingest 链 (spool-worker / cli ingest)
              corpus_prep.clean (第一道闸, 不变)
              autodream._build_segments (N4 预算分段, 不变)
              ├── [System 1: Laya] 提取前过滤 (E1, 新增闸)
              ├── [System 2: LLM] 提取 S-P-O + evidence (不变)
              ├── 槽位归一 (E2, 新增 — D1 诉求的正确承载):
              │     subject/object 实体槽 → resolver 两步闸
              │       step2 裁判位 [System 1: Laya] choice (P5)
              │     predicate 槽 → [System 1: Laya] 封闭词表归一 (E3, 低置信保留原词)
              │     value 槽 → 确定性 normalize (lowercase/trim, 纯规则非 Laya)
              ├── 矛盾裁决 [System 1: Laya] noul ← 替换 providers[0] LLM 位 (P2)
              │     multivalue/同值快路径保留 (主干不动)
              └── 语义边发现 [System 1: Laya] score (P4, 新增能力)
                    → fact_relations 表 (单向写, 读侧时态过滤)

夜间 dream (systemd daily timer, autodream 六职责)
              consolidate: LIF 衰减 + dedup + 退休 (纯SQL 确定性, **不变**)
                分组键 (subject_id, predicate, object_key) 哈希全等 — 永不 Laya 化
              dream.run_cycle / projection → MEMORY.md (不变)

recall (UserPromptSubmit → recall_inject)
              A路: 实体锚定检索 (纯程序, 永不经 gate, 不变)
              B路: wings 扩边 + 语义边 (P4, 确定性图遍历, 不变)
              ├── [System 1: Laya] Gate 判定 (P1) — v1.7③ 落地, 批量单次调用
              │     gate.run_gate LLM 位 → run_gate_laya
              └── 注入 <memsvc-recall> 标记块 (不变)
```

**v1.0→v1.1 架构修正**: 语义边/consolidate/投影不再画进 PreCompact 同步链(现实: spool 异步 + autodream daily);**§四 Step3 合并聚类(cluster_score)删除**——理由见 §四末"D1 裁决"。

---

## 一、Gate 判定 (P1, spec ③ 落地, 交互路径)

### 形态: 单次批量调用, 每候选 1 个 score question
- state = Query + `[fid] cand_text` 逐行;questions = 每候选 1 个 score(3 档 low/medium/high)。
- **概率双产出**(一个 question 顶两个):
  - `keep = probabilities["2"] >= 0.35`(P(high) 即相关性置信)
  - `match_score = norm_score(a, 3)`(期望值/2.0, 喂现有 bump_gate_score/解锁账本不变形)
- **matched_anchor 程序化补回**(v1.7③ 硬约束): anchors = A 路 fact 的 subject 名/别名集合, 本地子串匹配 cand_text;keep 但锚不上 → keep=False("高分布上锚不上 = 判定无效")。
- 8 候选 = 8q 单次调用 ≈ 350ms。
- 接缝: `gate.py` 新增 `run_gate_laya`, recall.py 调用点二选一(laya 可用→laya 版);入账(bump_gate_score)/scope/M3 软惩罚/b_wing_ids 判定零改动。
- 降级: 整批失败 → verdicts=None → **B 翼全不入, 只注入 A**(v1.7③ 原失败语义, GateFailed 语义不动)。

### 验收(v1.0 修正)
- 8 候选单次调用 **<1s**, hook 20s 预算占用 <5%(v1.0 "<100ms" 基于恒定 42ms 误设, 删除);
- 50 对标注准确率>80%(**前置依赖: 标注集需先建**);
- 挂掉自动降级只注入 A。

---

## 二、矛盾裁决 (P2, ingest 侧)

- 接缝: `llm_provider.py` 新增 `LayaJudgeProvider.judge_contradiction`(1 noul: "Should old be superseded by new?", `>=0.5` → supersede)。
- **组合式三层降级**: Laya → 同对象内 fallback(原 LLM provider)→ ProviderCallError → 外层 `_judge_contradiction` 现有 except → False(不 supersede, A1 fallback 契约)。
- `autodream._judge_contradiction` 主干与 multivalue/同值快路径**零改动**。
- 验收: 与 LLM 裁决 100 对一致率>85%。

---

## 三、提取前过滤 (P3 / E1, autodream 非交互)

- 全部 segments 一次批量(每段 1 noul: "contains extractable facts?"), `noul < 0.4` 段跳过 LLM 通道(regex 通道仍跑保底)。
- 整批失败 → 不过滤。corpus_prep.clean(第一道闸)/N4 分段预算不动。
- 验收: 不漏 fact(召回 100%);噪声过滤>30%(v1.0 "预估减少 30-50%" 无测量依据, 改为验收实测口径)。

---

## 四、语义关联发现 (P4/P5, autodream/daily 非交互)

### Step 1: 实体消歧 (P5) — subject/object 实体槽
- 接缝: resolver **step2 裁判位**(向量 top-k 召回后): `LayaJudgeProvider.dedupe_entity` — 1 choice(criteria={canonical: desc}), `confidence >= 0.5` 命中并入 alias, 否则不合并。
- state **必须带 context**(resolver 已透传, D-B b)——裸名实测 confidence 0.06 必不过阈(v1.0 验收案例 "memsvc→memory-service" 改为"带 context 重测")。
- step1 廉价闸(find_entity_exact)/step3 新建/向量召回管道零改动。

### Step 2: 语义边发现 (P4)
- 每 fact **一批**(20 neighbors = 20 score q, 4 档, 实测 1.5s;daily 窗口 10 fact ≈ 15s 可接受, 不做 200q 激进合批);`norm_score(a,4) >= 0.7` 入边。
- `fact_relations` 表: PK(source_id, target_id, edge_type) + target 反向索引;**单向写、读侧 OR 双向读**。
- **生命周期 = 读侧时态过滤**(JOIN fact 两端 status='active' AND valid_to IS NULL, 与 _temporal_clause 同构): fact 被软删(supersede/merge/prune, 仓内无物理 DELETE)后其边自然消失——不做 DELETE 级联。
- BFS 接入: `_build_entity_graph` 并边(nx 无向图天然双向);BFS/centrality/M3 遍历零改动。

### Step 3(过期检测 staleness_check)
- 保留, 挂 dream 侧(daily), 非 consolidate;形态同 v1.0(1 noul/fact)。**deferred 到 P4 验收后另票**(收益未证, 优先级最低)。

### D1 裁决(2026-09-29): 合并聚类(cluster_score)删除, 诉求收编到写入时槽位归一

v1.0 §四 Step3 `cluster_score_laya`(两组 fact "意思相近就合并")**删除**。理由:

1. **诉求已被前置覆盖**: 三槽位的语义归并本就由 resolver 承载(subject/object 实体槽, 留 alias 可回溯);cluster_score 只剩"谓词异写 + value 字面异写"两个小角。
2. **错误形态不对称**(裁决核心): 前置槽位判错 → 保守拒绝(不归一/不合并), 零损害可逆;fact 层合并判错 → max 吸收 + extract_sessions 并集**物理混账**, supersede 只回滚状态拆不回账本——夜间自动跑, 错误静默累积, 且伪造 spread 信号可骗解锁。
3. **与既有设计冲突**: consolidate 分组键 = (subject_id, predicate, object_key) 哈希全等是确定性根基("纯SQL 不变"承诺);概率配对 = 根基破拆。

**收编形态**(v1.0 的业务诉求"相近记忆别存两份"由以下承载):
- **E3 · predicate 槽归一(新增, 可选)**: 写入时 Laya choice 把新谓词映射到封闭谓词表("utilizes"→"uses");低置信 → **保留原词不归一**(零损害);归一后谓词参与夜间哈希判等, 合并仍确定性。
- **value 槽确定性 normalize(纯规则, 非 Laya)**: lowercase/trim/连字符变体归一("SQLite-vec"→"sqlite-vec");语义级归一不在此做——需要语义归一的 value 本该被提取为实体走 resolver 通道(提高实体化率是提取 prompt 侧治理, 非 Laya)。
- 漏合并残余代价有限: 多一条相似 fact, recall 排序自然竞争, 无害。

---

## 五、改动清单(对齐 temp/laya-integration-changelist.md)

### 新增
- `laya_client.py`: base URL(A4 修正)/health 探测+进程级 TTL 缓存/`laya_batch`(8k token 分片, 整批失败→None)/`norm_score` 单点归一
- `semantic_edges.py`: Step 2 边发现
- `gate.py::run_gate_laya` / `llm_provider.py::LayaJudgeProvider`(+dedupe_entity)
- schema: `fact_relations` 表
- 测试 5 件套: test_laya_{client,gate,judge,filter,semantic_edges}.py + dedupe

### 修改(主干零改动红线)
- `gate.py`(+run_gate_laya) · `recall.py`(gate 调用点 ~8 行 + 图并边 ~10 行) · `llm_provider.py`(+LayaJudgeProvider) · `cli.py`(providers 注入 ~4 行) · `autodream.py`(段过滤 ~6 行 + 边收尾 ~8 行 + E3 谓词归一挂点) · `db.py`/`schema.sql` · `store.py`(边原语) · `.env.example`
- **零改动**: resolver.py · consolidate.py · `_judge_contradiction` 主干 · gate 入账链 · `_build_segments` · corpus_prep

### 统一降级
```
laya_available(): /health 探测(进程级 TTL 60s 缓存, base URL 无 path)
Gate→只注入A | 矛盾→LLM→False | 过滤→不过滤 | 语义边→跳过 | 消歧→不合并 | 谓词归一→保留原词
```

### env
`MEM_LAYA_URL`(缺省 8190) · `MEM_LAYA_ENABLED`(总开关, 0=现状) · `MEM_LAYA_FILTER`(P3)

---

## 六、验收标准

| 集成点 | 验收 |
|---|---|
| Gate | 50 对标注准确率>80%(标注集先建);8 候选单次调用 <1s;挂掉自动降级只注入 A |
| 矛盾裁决 | 与 LLM 裁决 100 对一致率>85% |
| 提取过滤 | 不漏 fact(召回 100%);噪声过滤>30%(实测口径) |
| 语义边 | BFS 可达;20 条新边人工相关性>70%;软删 fact 的边读侧消失 |
| 实体消歧 | 带 context 的 memsvc→memory-service 重测通过;低置信不合 |
| 谓词归一 E3 | 封闭词表映射准确抽检;低置信保留原词(零误归一) |
| 整体 | dreaming 延迟增<2s→修正为 daily 窗口总增<60s(10 fact 语义边 15s + 过滤/边/归一批);**484+新增测试全过**(v1.0 "348" 为 main@W1 旧基线, 已过时) |

---

## 七、实施顺序

```
P0: laya_client.py + 批量原语 + norm_score          ← 地基
P1: Gate 批量 (spec ③ 落地)                          ← 交互路径, 改动<80行
P2: 矛盾裁决 provider 组合降级                        ← 改动<50行
P3: 提取前过滤                                        ← 改动<10行
P4: 语义边 + fact_relations schema                   ← 新增能力
P5: 实体消歧 (resolver step2 裁判位)
P5.5: E3 谓词归一 (可选, 封闭词表)
每 Phase 独立可测可回退; 全量 pytest(484+)不降为准出条件。
```

---

## 附: v1.0→v1.1 变更日志

1. [A1] score 归一 `/3.0`→`norm_score`(3 档/2.0);全文统一单点。
2. [A2] 实体消歧验收改"带 context 重测"(裸名实测 confidence 0.06)。
3. [A3] Gate per-candidate 循环 → 单次批量 8q;接缝改 provider/run_gate_laya 位;验收 <100ms→<1s。
4. [A4] laya_available URL 拼接 bug 修正(base URL)。
5. [B1] 架构图按真实链路重画(PreCompact=spool 异步;consolidate/边=daily)。
6. [B2] **Step3 cluster_score 删除**——consolidate"纯SQL 不变"矛盾消除(见 D1 裁决)。
7. [B3] matched_anchor 程序化补回(v1.7③ 硬约束)。
8. [B4/B5] 矛盾裁决/实体消歧接缝改 provider 位组合降级。
9. [B6] fact_relations 读侧时态过滤 + 单向写双向读。
10. [C1-C6] 延迟/基线/标注集/health 缓存/neighbors 来源全部落实测或前置依赖口径。
