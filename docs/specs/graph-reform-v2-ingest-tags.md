# 图改造 v2: 一步式 ingest + 分层 tag 索引 (2026-10-01 裁决定稿)

- v1 (e6a6b8c): chunk 原子化 + 开放边 + G1-G4。G1/G2 落地后回滚, 全量价值蒸馏进行中
  (temp/full_*: A致化 8635/B embed/C 分拣/D 审计/E atoms 4361/F 验边中)
- v2 叠加两需求: (a) tag 索引层作为图上层拓扑 (b) ingest 一步化, 拆掉 filter→consolidate 分步

## 一、模型

**递归三层(允许 L3+, 硬帽 ≤4)** — 裁决#1:

```
事实 tag 层    [repo:dsh] [repo:pipecat-poc] [svc:callback-bridge]   ← 必存骨架, 裁决#2
                    │ parent_of
语义 tag 层    [派发链] [网络代理坑] [GPU部署] ... (可再嵌套子方面)   ← 开放命名
                    │ indexes(w)
atom 层        知识原子 (fact/judgment/experience/summary + p_dur + 溯源 + valid_from)
```

- **事实tag必存**: 从 atom 溯源 (source_refs 的 session/cwd/repo) 直接铸币, 不过语义判别
  → 任何 atom 至少挂一个事实tag, 召回下界有保证; 语义tag与事实tag混合共存 — 裁决#2
- tag 节点: `{name, level, description一句, embedding(描述), members}`
- 边型: `indexes(tag→atom, w=laya挂载分)` / `parent_of(tag→tag)` / tag 间 `related`(共成员密度)
- 层级涌现: 自底向上跑社区发现, 对商图(上一层节点为点)迭代, 模块度增益 < 阈值即停

## 二、ingest 一步化 — 裁决#3 (spool+daemon)

```
PreCompact hook → spool(JSONL: 段+session/cwd/ts) → mem_daemon 消费(offset 幂等)
  → 切句(chunk_graph.split_units 复用)
  → zhipu v4 判据一步: 致化+五类标签+段内 supersede (强判据: 可查回性/主语资产性)
  → embed(本地 LMStudio qwen3)
  → 对既有 atoms 查 cos: ≥0.90 并成员 / 0.80-0.90 记候选边
  → laya 一步: p_dur 审计 + 候选边验证 (单调用, state=新句+邻居atoms)
  → 直接入图 (label/p_dur/valid_from=now/溯源)
```

- 崩溃安全: spool 即持久队列, 段级 ack
- 跨段 supersede/证伪 → 挂账 → 夜间清算 (写 valid_to, D4 双时态列已在库)
- 写侧裁决唯一源 = zhipu v4 判据 + laya; sys1 (laya) 移到写时闭合

## 三、过滤通道全面退役 — 裁决#4

- T3 (autodream 逐段 noul 0.25 过滤) 删除
- regex/gazetteer 老通道删除 (v1 "降级为仅产归属信息" 作废; 归属改由事实tag铸币承担)
- 一切硬编码/dict 式过滤由 laya sys1 类模型裁决替换

## 四、dreaming 换岗 (夜间轻量)

不再过滤/合并事实, 只做:
1. tag 层维护: 新 atom 试挂既有 tag(embedding top-3 + laya), 挂不上触发新社区发现
2. supersede/证伪清算: 挂账对 + 强边两端互为正反命题的检测 (F 数据已见"立项vs证伪"对)
3. 度数异常重组: hub >20 边触发局部重聚

## 五、实施票 (对抗校验 wf_a8f9bcd1 修正版)

| 票 | 内容 | 依赖 | 验收 |
|---|---|---|---|
| H0 | **laya 运维接线**: .env 落 MEM_LAYA_ENABLED=1 + fail-fast 探活; daemon 定为**唯一 laya 调用方**(其余进程经 daemon 队列); laya 不可用→spool 积压持有(不裸入图), 恢复后补审; 容器资源限额 | — | 拔 laya 容器→ingest 挂起不丢段; 恢复→自动补审 |
| H1 | **切换票(不是新建)**: hook 已在落 spool(整 transcript 快照)。本票=一次性切换: 旧 spool-worker→endsteps→autodream 旧通道下线 + 新段级 JSONL 格式 + daemon 消费接上, 三者同一变更窗口 | H2,H7 就绪 | 切换窗口零双写; 旧 worker 不再拉起; test_precompact_spool 契约更新 |
| H2 | distill 模块产品化(v4 判据+embed+laya+入库); **段内容 sha 去重**(跨文件重放免疫); 毒段 DLQ(max-attempts+退避); embed 失败→atom 标 needs_embed, 夜间补扫; 超时用线程池硬帽(zhipu 120s/laya 60s), 不阻塞 daemon 主循环 | H0 | 幂等重放零重复计费; DLQ 有界; daemon 不被单调用饿死 |
| H3a | 写侧退役: T3(autodream:499-529, 生产从未启用, 纯死代码) + chunk_graph._filter_units(:52-69) + test_laya_filter.py | H2 | pytest 绿 |
| H3b | 读侧 gazetteer 处置: gate.py:101 derive_keywords / surprise.py:124 novelty / store.py:214 计数器。gate 延迟敏感→换 embedding-topk 本地关键词, **不走 laya**; bootstrap 冷启动零 LLM 路径**待裁决#5** | H6 | 召回回归集无降级 |
| H4 | tag dreaming: **L1 改 embedding 聚类**(proto 阶段已验证形态), 不用 laya 边图做社区(实测边权 75% 落 0.3-0.5 近均匀带, 商图不可操作); F 完成后在强边子图上复测拓扑再定稿; hub 阈值按复测重定(现 11 个 atom 度>20) | F 完成 | 挂载 w 分布 + 层级涌现统计 + 复测报告 |
| H5 | 存量迁移: **新图 DDL**(atom/tag/atom_edge 表, fact 表转 legacy 归档不删) + delta 补账(快照后旧管道已 +109 fact 且在涨, 迁移时按 created_at 补蒸馏) + 快照→迁移→对账→回滚演练 | H4 | 对账零差; 回滚演练通过 |
| H6 | 召回面适配: cand_texts→atoms, tag 上下行遍历, gate 重校准 | H5 | 新标注集阈值实测 (G4 延续) |
| H7 | daemon 消费循环 + 夜间清算(supersede/证伪/补扫) | H2,H4 | 挂账清零; 证伪对抽检 |

## 六、事实tag铸币规则 (对抗校验修正)

实测: source_refs 只有 session: 形态, 无 cwd/repo; cwd 在独立列 fact.source_cwd 且存量 93% NULL; 55.8% 迁移 atom 铸不出 repo tag。修正:

- **保底**: session: tag 恒可铸(source_refs 必有) → "至少一个事实tag"下界由 session 承担
- **repo:/cwd:**: source_cwd 非空时铸, 归一规则: 已知根白名单映射(~/projects/* → repo:名), /tmp 等临时目录跳过; svc: 分类暂缓, 不从 cwd 硬推导
- 新 ingest 路径 spool 载荷自带 cwd → 增量侧 repo tag 覆盖率趋满

## 七、对抗校验裁决记录 (wf_a8f9bcd1, 2 agent/55 tool calls)

- 🔴 H1 窗口期旧 worker 会把新格式文件当空蒸馏删除(静默丢记忆) → H1 改为一次性切换票
- 🔴 laya 生产 env 未启用+静默跳过+三服务 AND 依赖 → 新增 H0; laya 不可用=挂起非裸入
- 🟡 事实tag 铸币字段假设破产(§六修正)
- 🟡 跨文件重放 O(N) 计费/毒段无 DLQ/embed 静默失败无修复 → H2 三件套
- 🟡 边图社区发现在实测权重分布上不可操作 → H4 改 embedding 聚类
- 🟡 H3 退役面比 spec 宽(gate/surprise/bootstrap/store) → 拆 H3a/H3b
- 🟡 H5 缺 DDL/delta 补账/旧写通道退役 → H5 扩容
- ✅ 确认锚点: split_units / valid_from,valid_to 列 / hook 现有 spool 行为 / mem_daemon offset 机件

## 八、裁决记录 (2026-10-01)

1. ✅ tag 层级允许递归 (不固定两级)
2. ✅ 语义一致, 事实tag必存, 语义/事实混合共存
3. ✅ ingest 走 spool+daemon 异步
4. ✅ T3/regex/gazetteer 过滤全面退役废弃, laya sys1 模型方式全权替换硬编码+dict 过滤
   (校验修正: 写侧全替换; 读侧 gate 延迟敏感路径换 embedding-topk 本地方案, 不强上 laya)

## 九、裁决 #5 (2026-10-01)

- ✅ b) bootstrap 冷启动接受联网依赖: 零 LLM 本地档退役, 冷启动需 zhipu/laya 可达。
  test_coldstart_unlock/test_fallback_lane/test_bootstrap_skip 行为随 H3b 重写为"不可达即挂起等恢复", 不再保留离线产记忆路径
