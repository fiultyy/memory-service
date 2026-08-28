# memsvc 架构概述与指针文档

memsvc（mem-service）是一个独立 Python 记忆服务：把对话事实与实体沉淀为 KG fact 层（Entity + Fact reified + 正交元数据），并按需加权召回。存储自治（SQLite WAL，`data/memory.db`），无常驻服务进程——形态是短命 CLI 进程直连库，用完即退。

## 一、分层总览

```
接入面   skills/memsvc（软链三处: repo 正本 / ~/.dsh/skills / ~/.claude/skills）
命令面   cli.py 20+ 子命令（唯一写入口口径；stats/recall 为读面）
管道层   endsteps → transcripts → autodream → gazetteer/llm_extract → resolver → store
存储层   store.py + db.py + schema.sql（SQLite WAL, vec0 向量表, upgrade_queue, signals）
召回层   recall.py + scoring.py + vec_index.py + embedding.py
投影层   projection.py + hygiene.py（KG → CC memory md, 单向）
常驻面   mem_daemon(dream-daemon) + graphlive.py(M20 实时图, 按需手动拉起)
自动面   hooks/pre-compact-mem.sh + spool-worker.sh（唯一自动面, CC 端）
```

## 二、模块指针（文件 → 职责）

| 模块 | 职责 |
|---|---|
| `cli.py` | 全部子命令入口：ingest / recall / consolidate / autodream / ingest-recent / init-memory / re-ingest / synthesis-index / prune / write / confirm / invalidate / elevate / cite / stats-json / dream-daemon / graph-export / graph-live |
| `store.py` | Entity + Fact CRUD（ADR-2/3）：put_entity / put_fact / update_fact_status；per-fact 原子写 |
| `db.py` | 连接管理 + schema 迁移（列检测 ALTER 补齐）；`db.init(path)` 可切换库（测试用） |
| `schema.sql` | 表定义：entity（name+type 唯一约束、aliases、name_embedding）、fact（LIF 五维、bi-temporal valid_from/to、status、supersede 链、provenance/veracity、source_cwd、topic） |
| `endsteps.py` | 蒸馏闸单源：只留 assistant 轮末 text 块（≥120 字、轮末 gate、侧链排除、文内去重）；CC/dsh/pi 三家同 env 单源 |
| `transcripts.py` | 多 harness transcript 统一定位/蒸馏（M19）：cc=`~/.claude/projects`、dsh=`~/.dsh/sessions`、pi=`~/.pi/agent/sessions`；zstd 自动解压 |
| `autodream.py` | transcript → KG 增量（ADR-10/11）：ADD/UPDATE/DELETE/NOOP 幂等决策；块文法继承 provenance（M8） |
| `gazetteer.py` | M7 占位提取器：KG 自举词典 + regex 三路，零 LLM inline，extractor=regex（信任档 0.4） |
| `llm_extract.py` | LLM 直抽通道（batch 12）：glm-5-turbo + emit_extraction 工具 + prompt v3；两次 schema 失败响亮失败 |
| `adapter.py` | 蝴蝶翼 LLM 抽取（ADR-5b）：N=3 fan-out + 投票 quorum，ingest 子命令走此路 |
| `llm_provider.py` | LLMProvider Protocol + ZhipuAnthropicProvider |
| `resolver.py` | D3 两步实体合并：精确/别名闸 → 向量 top-k + LLM 去重；跨 ingest 同实体异写消解 |
| `predgate.py` | 谓词聚边（batch 13）：开放谓词词汇 + 近似度聚类，压 connected_to 大锅饭 |
| `consolidate.py` | LIF 五维重算 + 精确重复合并（ADR-8v2/ADR-6）；type-aware decay：ephemeral 7d / stable 90d / permanent ∞ |
| `recall.py` | KG 召回：词法 seed → 候选 → 打分；--vector/--bfs/--as-of/--cwd/--json/--project |
| `scoring.py` | `score = α·match + β·centrality + γ·LIF + δ·vec_sim`（ADR-4v2）；on-the-fly pagerank |
| `embedding.py` | OpenAI-compat REST 嵌入客户端（LM Studio 127.0.0.1:16666 qwen3-embedding-4b） |
| `vec_index.py` | sqlite-vec 向量索引面：vec_entity/vec_fact 虚表 + 回填 |
| `projection.py` | KG → CC memory 投影（ADR-15）：mem-*.md fact 卡 + recall --project 日志（ADR-16f 防自指） |
| `hygiene.py` | M12 投影卫生轮：零 LLM 三动作（superseded 退场 / MEMORY [mem] 重排） |
| `dream.py` | M11 dreaming 整合层：信号重放 LIF 补回 / fact_type 晋升降级 / D9 参数提案 / 复述回流压档 / wings 升级消费 |
| `mem_daemon.py` | dream-daemon：常驻循环（日频 dream + 时频卫生），operational #1 |
| `signals.py` | M5 信号流：confirm_arrivals / citations / recall_hits / agent_crud / escalation 五流 append-only |
| `surprise.py` | M9 惊喜度：升级队列 upgrade_queue 的优先级源 |
| `graphlive.py` | M20 实时图：ctypes inotify 盯 wal（零轮询）+ rowid 游标增量 + SSE + graph-export |
| `web/graph_live.html` | sigma.js 单页：类型/度数过滤、时间窗、搜索聚焦、SSE 增量生长 |
| `hooks/` | pre-compact-mem.sh（CC 唯一自动面）+ spool-worker.sh（endsteps 蒸馏进提取）+ 其余三钩子休眠 |

## 三、数据流

进端（写）：transcript → endsteps 蒸馏（只留 assistant 轮末结论段）→ autodream → 提取器（gazetteer 占位即时入库 / wings LLM 异步升级经 upgrade_queue+surprise 排队 / ingest 子命令蝴蝶翼直抽）→ resolver 合并实体 → store 落 fact（provenance 随源块：user_prose 1.0 / tool_obs·human 0.9 / agent_assert·system 0.5）。

出端（读）：recall query → 分词 → entity.name LIKE 锚定 seed → 候选（subject/object 邻域）→ α·match + β·centrality(pagerank) + γ·LIF + δ·vec 打分 → top-k；`--bfs` 图扩展补字面盲区（入图门槛 lif_source≥0.7）；`--as-of` bi-temporal 点时回放。

实时图（M20）：inotify 盯 data/ 目录 wal/shm/db 事件 → 去抖 250ms → rowid 游标增量（端点并集补发防悬空边，degree>0 过滤）→ SSE 推送 → sigma.js 增量加点/边。graph-export 产出 Cosmograph/Gephi Lite 口径 csv。

## 四、关键语义与红线

- 幂等：autodream/init-memory/re-ingest 重跑 fact 级 NOOP 吸收；ingest-recent 有 sha256 注册表防重跑。
- 无 delete：物理删除不存在（P38）；过时走 supersede 链（bi-temporal 可回溯）。
- 通道判定（DR-9）：agent 不可声明 human 档；provenance 随源块自动映射。
- 唯一自动面 = PreCompact（CC）；recall/consolidation 手动；dsh/pi 进端手动（钩子桥无 PreCompact 缝）。
- 例外（用户裁决 2026-08-27）：recall --project 把召回结果投影成 memory/recall-<DATE>.md + MEMORY.md 索引行。
- 图语义边界（用户裁决 2026-08-28）：图不连通只能读作「库内查无记录」，不是现实无关的证明。
- 复述禁令：召回内容不复述原文（防提取管道回流污染，U7）。
- LIF 五维：freq/recency/spread/coherence/source 复合成信任标量；LIF<0.1 翻 deprecated。

## 五、部署指针

- 正本：`/home/yy/projects/memory-service/`；调用约束：`cd` 到服务目录或用绝对路径（模块裸 import，脚本目录自动进 sys.path，任意 cwd 可用）。
- 软链：`~/.dsh/skills/memsvc`、`~/.claude/skills/memsvc`（pi 经 claude 兼容发现层扫 ~/.claude/skills 零安装）。
- 依赖门：词法召回零依赖；--vector/--bfs 需 LM Studio 16666；入库类走 glm-5-turbo（.env ZHIPU_API_KEY），LLM 断供响亮失败绝不回落 regex。
- 实时图：`cli.py graph-live --port 8766`（默认 8765 被 rt_gateway 占用）；部署裁决 = 不常驻，按需手动拉起，无状态秒起。
- 迭代史：`docs/mem-service-iteration-log.md`（M 系列 + 接线裁决）；实现 ledger：`~/research/LEDGER-memsvc-impl.md`。
