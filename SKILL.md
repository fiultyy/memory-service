---
name: mem
description: "mem-service 记忆接入:把对话事实/实体落 KG fact 层 + 按需召回相关 Fact。叠加在 CC 现有记忆之上(不改 MEMORY.md)。触发:记忆/recall/ingest/查历史事实/落记忆/结构化记忆/mem service。"
---

# mem — mem-service KG 记忆接入(叠加层)

mem-service 是独立 Python 服务,在 CC 现有记忆(~/.claude memory md,即时派热/温层)之下**叠加**一层 KG fact(Entity + Fact reified + 正交元数据)。**绝不改 CC memory md**——skill 是唯一桥梁,通过 cli 读写服务自己的 SQLite KG [ADR-7]。

形态: **CC 按需调 cli**(用户手动触发,或你读完本 skill 后主动调);PreCompact hook 在 `/compact` 前自动把 session transcript 抽成 KG 增量(ADR-10/11)。

> 接线/部署详见 [INSTALL.md](INSTALL.md),能力与迭代详见 `docs/mem-service-iteration-log.md`。

---

## 触发条件

关键词: `mem` / `记忆` / `recall` / `ingest` / `查历史事实` / `落记忆` / `结构化记忆`

或任务特征:
- 用户让你"记住"某事实/偏好/决策
- 需要查跨会话的结构化事实(谁用了什么 / X 依赖 Y)
- 长任务里散落的事实需要持久化进 KG
- 显式说"用 mem" / "调 memory service"

**不触发**: 即时对话上下文 CC memory md 已覆盖(本 skill 不替换它)。

---

## cli 路径与调用约束

服务源在 `/home/yy/projects/memory-service/`(独立项目, cli 在顶层),cli 入口 = `cli.py`。**cli 直接 import 同目录模块(`adapter`/`autodream`/`bootstrap`/`consolidate`/`recall`/`store`/`resolver`/`llm_provider`),不是 Python 包,必须在服务目录下用 `python3 cli.py` 调,不能从别处 import。**

部署形态(2026-08-07 从 AO2 `services/memory-service/` cp 独立后):
- **源码**: `/home/yy/projects/memory-service/`(cli 在顶层)
- **CC deploy(已就位)**: 软链 `~/.claude/skills/mem → /home/yy/projects/memory-service` ← CC `/mem` 走软链读源码,无需拷贝

ADR-7: skill 源即独立项目根, 软链是 deploy 形态, 本文件就是源。

### 调用方式

```bash
# deploy 后(软链, 已就位 — 推荐):
python3 ~/.claude/skills/mem/cli.py <subcommand> ...

# 或直接源码绝对路径:
python3 /home/yy/projects/memory-service/cli.py <subcommand> ...
```

**注意** cli 内部是同目录裸 import,**实际只接受 cwd = 服务目录 或 把服务目录加进 PYTHONPATH**(绝对路径调若 cwd 不对会 import 失败)。最稳的调法:

```bash
cd /home/yy/projects/memory-service && python3 cli.py <subcommand> ...
# 等价: cd ~/.claude/skills/mem && python3 cli.py <subcommand> ...
```

---

## 子命令契约(严格对齐 cli.py)

16 个子命令(详见 `cli.py _main`):原 11 个 + 四动词 `write` / `confirm` / `invalidate` / `elevate` + `cite` + `stats-json`。

CC 以 `mem` 为调用名(skill 名 = `mem`, 软链到 `~/.claude/skills/mem/`)。面向 CC 的调用 = `mem <subcommand>`,底层 = `cli.py <subcommand>`,两种写法等价:

```bash
mem ingest "<text>"                        # CC 调用名(软链 deploy 后)
python3 cli.py ingest "<text>"             # 底层 cli(cwd=服务目录)
```

### 1. `ingest` / `mem ingest` — LLM 抽实体+事实入 KG

```bash
mem ingest "<text>" [--source <ref>] [--fact-type stable|ephemeral|permanent]
# 等价: python3 cli.py ingest "<text>" [--source <ref>] [--fact-type <t>]
```

- 蝴蝶翼 LLM 抽取(ADR-5b,N=3 并行 fan-out + 投票 quorum ⌈n/2⌉)抽 Entity + Fact;**无 regex 降级**(LLM 不可用即 `RuntimeError` block)
- `fact.extractor` 标 `"vote"`(蝴蝶翼≥2翼投票);实体经 `resolver.resolve_entity`(ADR-D3 两步合并:精确/别名闸 → 向量 top-k + LLM 去重 → 创建)解析,重抽复用既有 id。`fact.confidence` 带投票聚合置信度,初始 LIF 五维即时算(非延迟首次 consolidate)
- `--source`: 可选来源引用(如会话 id / 文件路径),写进 `fact.source_refs`
- stdout: JSON `{"entities": <n>, "facts": [<fid>, ...]}`

**示例**:
```bash
$ python3 cli.py ingest "用户使用 rust 进行开发"
{"entities": 2, "facts": ["a962a25ffe6644eabf56ab1c6457d560"]}
# → Fact(subject=用户, predicate=uses, object=rust, extractor=vote)
```

### 2. `recall` / `mem recall` — KG 加权召回 Fact

```bash
mem recall "<query>" [--verbose] [--vector] [--bfs] [--gate|--no-gate] [--as-of <ts>] [--cwd <cwd>] [--session <sid>] [--top-k <n>]
```

- 加权召回(ADR-4v2): `score = α·match + β·centrality + γ·LIF + δ·vec_sim`——字面匹配 + PageRank 中心性 + LIF 信任标量 + 向量相似度
- `query` 经分词后 `entity.name LIKE %token%` 定位 seed 实体 → 其 subject/object 的 Fact 为候选集
- `--vector`: 向量召回融合(ADR-13,解同义/改写/字面盲区)
- `--bfs`: BFS 图遍历召回(D5,召回图近但字面/向量远的 fact);`--bfs-hops`(默认 2)/`--bfs-scoped`(限本 cwd 图)。**软惩罚**(v1.7③ M3 终裁): B 翼扩展 fact 全部入场,排序分乘 `gate_mod=0.5+0.5·min(1, lif_source/0.7)`(regex 0.4 档≈0.786 折,≥0.7 档不打折);折后分仍绕 0.3 噪音地板入榜,仅排序降权;主检索路径(A 路)不受乘子影响
- `--gate`(默认开,`--no-gate` 逃生): v1.7③ 对 B 翼扩展 fact 跑单 LLM 一致性 gate(query 自动升格 `{keywords, intent, scope:"manual"}`,与注入面同一 gate schema 零分叉)。判 keep 的 fact 附 `gate_keep:true`+`match_score` 键;判不匹配或 gate LLM 不可用(断供/超时/两轮 schema 败)→ **B 翼全部不入返回,只返回 A 路**(不降级不静默当 keep,recall 不炸)。手动面 match_score 不入 gate_score 解锁累计(防 CLI 探测污染账本)
- `--as-of`: 点时召回(bi-temporal,只返回 `valid_from<=t<valid_to` 的 fact)
- `--cwd`: ADR-14 过滤 source_cwd(本 cwd fact + NULL 老数据;默认全 cwd)
- `--verbose`: 每条 Fact 追加 `_scored`/`_subject_name`/`_object_name` 调试字段
- `--json`: M15a 稳定 JSON 契约输出 `{"query", "facts":[…]}`(见下)
- `--session`: 可选 session id(默认 `CLAUDE_CODE_SESSION_ID` env),用于 LIF 刷新记录 `seen_sessions`/access_count
- stdout: JSON Fact 数组

**示例**:
```bash
$ python3 cli.py recall "rust"
[{"id":"...","subject_id":"...","predicate":"uses","object_id":"...","value":"rust","LIF":0.58,"extractor":"vote","confidence":0.7,"status":"active",...}]

$ python3 cli.py recall "rust语言" --vector   # 向量召回解同义
$ python3 cli.py recall "rust" --verbose      # 带 _scored/_subject_name/_object_name
$ python3 cli.py recall "rust" --json         # 稳定契约 shape(见 §JSON 契约)
```

#### `--json` 稳定输出契约(M15a,字段名即 ABI——变更须留 changelog)

```json
{"query": "<原查询>", "facts": [
  {"id","subject_id","predicate","object_id","value",
   "fact_type","LIF","status","provenance","veracity","topic",
   "extractor","supersede_reason","supersedes_id",
   "valid_from","valid_to","created_at",
   "access_count","last_accessed_at","score"}
]}
```

- `facts` 列表序 = recall 输出序(score 降序);缺列投 `null`(老数据 NULL=legacy)
- `provenance`(出处轴): user_prose|tool_obs|agent_assert|human|system;`veracity`(权威标量)随出处自动映射
- 供 agent/脚本消费;`stats-json` 同为契约 shape

### 3. `consolidate` / `mem consolidate` — decay + 去重

```bash
mem consolidate
# 等价: python3 cli.py consolidate
```

- 两阶段(ADR-8v2 + ADR-6):先 LIF 五维重算(`compute_lif`,decay 折叠进 recency dim: `exp(-ln2·age_h/half_life)`,type-aware:ephemeral 7d / stable 90d / permanent ∞;`LIF<0.1` 翻 active→deprecated),再精确重复 Fact(`subject_id, predicate, object_id, value` 全同)合并,余者翻 `status=superseded`
- stdout: JSON `{"decayed": <n>, "deprecated": <n>, "superseded": <n>, "active": <n>}`
- 幂等:干净运行返回全 0

### 其余子命令(概览)

| 命令 | 作用 |
|---|---|
| `autodream --session <id> --transcript <jsonl> [--cwd]` | session transcript → KG 增量(ADD/UPDATE/DELETE/NOOP 幂等,ADR-10/11) |
| `init-memory --memory-dir <dir> [--cwd]` | CC memory .md → KG permanent 种子(ADR-12) |
| `re-ingest <file>` | 单 md → KG 增量(ADR-17) |
| `synthesis-index [--scope <cwd>] [--memory-dir <dir>] [--session <id>]` | 散 mem-*.md 对账 → MEMORY 投影(ADR-15 P2,唯一写入口) |
| `prune [--scope <cwd>] [--memory-dir <dir>]` | 删除 KG 中无对应 memory .md 的孤儿 fact |
| `embed-backfill` | active fact value → L2 向量 cache |
| `write <subject> <predicate> <value> [--fact-type] [--cwd]` | 四动词 write:新事实(provenance=通道档,信号 agent_crud) |
| `confirm <fact_id>` | 四动词 confirm:证实(信号 confirm_arrivals,P22 确认轴) |
| `invalidate <fact_id> [--note]` | 四动词 invalidate:失效建议(superseded+contradiction;human 路径交互确认) |
| `elevate <fact_id>` | 四动词 elevate:晋升提名(不动 fact,仅信号;human 路径交互确认) |
| `cite <fact_id> [--ref]` | M16 引用记账(citations 信号,单向正奖励,不碰 KG 写面) |
| `stats` / `stats-json` | 只读 churn 快照(entity/fact 计数 + status 分布;stats-json 为契约 shape) |
| `dream-daemon [--cwd] [--interval <s>] [--once]` | 常驻 autoDream loop(watch CC transcript 增长 → 增量 dream,operational #1) |

---

## 数据与状态

- KG 数据: `data/memory.db`(cwd=服务目录; SQLite,服务自治, ADR-7)
- 向量 cache: `data/embeddings.db`(L2 SQLite)
- schema: `schema.sql`(Entity + Fact reified,**无 MemoryItem 表**, ADR-2/3)
- 状态独立于 CC memory md;`synthesis-index` 单向投影回 MEMORY(ADR-15)

## 何时不该用

- 即时对话上下文(CC memory md 已是即时派热/温层,够用)
- 需要 per-turn 自动注入记忆(v1.7①② 已交付: UserPromptSubmit 注入器常驻, 首 n turn 绑定 B翼/gate 增强档)

---

## 维护动词时机教学(何时写记忆)

KG 是增量演化的:写入 → 召回验证 → dreaming 日频整合(晋升/升级/卫生)。你(agent)在写入侧的时机判断。**通道判定**(DR-9):物理 tty 且无 `MEM_AGENT_CONTEXT` env → human 档(provenance=human,veracity 0.9);否则 agent 档(agent_assert,0.5)——**agent 不可声明 human 档**(env 只能降档不能升档);invalidate/elevate 在 human 路径需交互确认(y/N)。

- **该记新事实**: 用户陈述了可复用的偏好/决策/事实("以后 X 用 Y"/"项目 Z 依赖 W"),或会话中挖出了跨会话有价值的结构化信息。走 `mem write "<subject>" "<predicate>" "<value>"`(通道自动判档: agent 调用 provenance=agent_assert)或让 PreCompact hook 自动抽取。
- **该确认**: 召回结果被实际采用且被验证为真 → `mem confirm <fact_id>`(确认信号入 confirm_arrivals 流,抬升信任档;P22 确认轴)。
- **该建议失效**: 发现召回内容已过时/错误 → `mem invalidate <fact_id> [--note "<说明>"]`(旧 fact superseded+reason=contradiction 时效标注)。
- **晋升提名**: 高价值事实值得长存 → `mem elevate <fact_id>`(不动 fact,仅记晋升偏好信号,裁决权在 dreaming LIF 阈值)。
- **引用记账**: 输出中采用了某 fact → `mem cite <fact_id> --ref "<输出引用>"`(citations 流单向正奖励,不碰 KG 写面)。
- **不该记**: 即时对话上下文、一次性操作细节、会被立即覆盖的临时状态——这些归 CC memory md,不进 KG。
- **无 delete/punish**: 物理删除不存在(P38)。过时内容走失效/取代(supersede 链保历史,bi-temporal 可回溯),删除是 human 专属的投影面操作。

## 查询策略(何时用哪个 flag)

| 场景 | 用法 |
|---|---|
| 精确知道实体名 | `mem recall "<name>"`(字面 seed,主路径) |
| 同义/改写/中英混排查不到 | 加 `--vector`(向量融合解字面盲区) |
| 知道 A 想找关联的 B(图近字面远) | 加 `--bfs`(可能 `--bfs-hops 2`);跨 cwd 噪声大时 `--bfs-scoped` |
| "当时我以为什么"(历史状态) | `--as-of <ISO ts>`(bi-temporal 点时召回) |
| agent/脚本程序化消费 | `--json`(稳定契约,字段名即 ABI) |

**env 语义**: `MEM_DELAYED_REINFORCE=1` 时 recall 是**纯读**(不即时写回强化),命中记入 `data/signals/recall_hits` 流,由 dreaming 日频批量补回 LIF——对调用方透明,输出 shape 不变;缺省(0/未设)为旧行为即时写回。

## 复述禁令(输出纪律)

**勿在输出中复述召回内容原文。** 复述回流是已知污染路径:被复述的召回文本若再次进入 transcript,会被提取管道重新捕获,dreaming 的回流检测(U7)将压降其升级优先级,并可能产生自述污染回声(「我记得…」类 fact)。

正确姿势:
- **转述 + 引用**: 用自己的话重述,并引用 `fact_id`(`kg://fact/<id>`)供溯源
- 摘要式消费,不整段照抄 `value`
- 引用记账(M16)将把被采用的事实记入 citations 流(单向正奖励)

---

## 实现边界(见 Spec §3/§9)

**已实现**:LLM 蝴蝶翼抽取(ADR-5b)+ `α·match+β·centrality+γ·LIF+δ·vec` 召回(ADR-4v2)+ 向量召回 `--vector`(ADR-13)+ PreCompact autoDream hook(ADR-10/11)+ type-aware decay(ADR-8v2)+ 多值谓词共存 + KG→CC 投影 `synthesis-index`(ADR-15 P2)+ BFS 图召回(D5, v1.7③ M3 终裁: B翼硬门槛摘除, 改软惩罚 gate_mod 乘子)+ bi-temporal 点时召回(D4)+ autoDream daemon `dream-daemon`(operational #1)。

**新实态(2026-08 统一记忆系统改造后)**:

- **占位-升级时序(M6/M7/M4/M9)**: autodream 主径用 **gazetteer 占位提取器**(KG 词典+regex 三路,零 LLM inline,`extractor='regex'` 0.4 档即时入库,provider 断供不中断写入);wings LLM 退役为**异步升级**——待升级素材入 `upgrade_queue` 表按惊喜度排队,dreaming 日频消费。ingest 子命令仍走 wings 直连。
- **块文法提取(M8)**: transcript 按 (block_type, text) 序列读取,tool_use/tool_result 不再丢弃;fact 继承源块 `provenance`(user_prose/tool_obs/agent_assert/human/system),`veracity` 随出处自动映射(user_prose 1.0 / tool_obs·human 0.9 / agent_assert·system 0.5)。
- **延迟强化(M5/M10)**: `MEM_DELAYED_REINFORCE=1` 时 recall 纯读+信号落盘(`data/signals/*.jsonl` 五流 append-only),LIF 重算移入 dreaming 批量补回;缺省旧行为不变。
- **dreaming 期(M11)**: `dream-daemon` 主循环日频(86400s 可调)跑 `dream.run_cycle()` 六职责——信号重放 LIF 补回 / fact_type 晋升降级 / D9 参数提案(只落 diff 供人审) / 复述回流压档 / 自述污染降档 / 队列 wings 升级(`supersede_reason='upgrade'`)。
- **投影卫生(M12)**: 卫生轮(3600s 可调)succeeded dreaming 同轮或独立跑:superseded/deprecated 投影退场、MEMORY [mem] 段按现值重排(零 LLM)。
- **源不变式**: 提取/升级数据源 = transcript 原文/队列素材,永不读自家 KG 作提取输入。

仍 defer:冷层类聚 / query 独立 cli / 跨 scope 向量联邦 / BFS_WEIGHT 调参(需 eval)。
