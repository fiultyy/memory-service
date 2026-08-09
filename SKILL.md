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

11 个子命令(详见 `cli.py _main`):`ingest` / `recall` / `consolidate` / `autodream` / `init-memory` / `re-ingest` / `synthesis-index` / `prune` / `embed-backfill` / `stats` / `dream-daemon`。

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
mem recall "<query>" [--verbose] [--vector] [--bfs] [--as-of <ts>] [--cwd <cwd>] [--session <sid>] [--top-k <n>]
```

- 加权召回(ADR-4v2): `score = α·match + β·centrality + γ·LIF + δ·vec_sim`——字面匹配 + PageRank 中心性 + LIF 信任标量 + 向量相似度
- `query` 经分词后 `entity.name LIKE %token%` 定位 seed 实体 → 其 subject/object 的 Fact 为候选集
- `--vector`: 向量召回融合(ADR-13,解同义/改写/字面盲区)
- `--bfs`: BFS 图遍历召回(D5,召回图近但字面/向量远的 fact);`--bfs-hops`(默认 2)/`--bfs-scoped`(限本 cwd 图)
- `--as-of`: 点时召回(bi-temporal,只返回 `valid_from<=t<valid_to` 的 fact)
- `--cwd`: ADR-14 过滤 source_cwd(本 cwd fact + NULL 老数据;默认全 cwd)
- `--verbose`: 每条 Fact 追加 `_scored`/`_subject_name`/`_object_name` 调试字段
- `--session`: 可选 session id(默认 `CLAUDE_CODE_SESSION_ID` env),用于 LIF 刷新记录 `seen_sessions`/access_count
- stdout: JSON Fact 数组

**示例**:
```bash
$ python3 cli.py recall "rust"
[{"id":"...","subject_id":"...","predicate":"uses","object_id":"...","value":"rust","LIF":0.58,"extractor":"vote","confidence":0.7,"status":"active",...}]

$ python3 cli.py recall "rust语言" --vector   # 向量召回解同义
$ python3 cli.py recall "rust" --verbose      # 带 _scored/_subject_name/_object_name
```

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
| `stats` | 只读 churn 快照(entity/fact 计数 + status 分布) |
| `dream-daemon [--cwd] [--interval <s>] [--once]` | 常驻 autoDream loop(watch CC transcript 增长 → 增量 dream,operational #1) |

---

## 数据与状态

- KG 数据: `data/memory.db`(cwd=服务目录; SQLite,服务自治, ADR-7)
- 向量 cache: `data/embeddings.db`(L2 SQLite)
- schema: `schema.sql`(Entity + Fact reified,**无 MemoryItem 表**, ADR-2/3)
- 状态独立于 CC memory md;`synthesis-index` 单向投影回 MEMORY(ADR-15)

## 何时不该用

- 即时对话上下文(CC memory md 已是即时派热/温层,够用)
- 需要 per-turn 自动注入记忆(三频 hook defer)

---

## 实现边界(见 Spec §3/§9)

**已实现**:LLM 蝴蝶翼抽取(ADR-5b)+ `α·match+β·centrality+γ·LIF+δ·vec` 召回(ADR-4v2)+ 向量召回 `--vector`(ADR-13)+ PreCompact autoDream hook(ADR-10/11)+ type-aware decay(ADR-8v2)+ 多值谓词共存 + KG→CC 投影 `synthesis-index`(ADR-15 P2)+ BFS 图召回(D5)+ bi-temporal 点时召回(D4)+ autoDream daemon `dream-daemon`(operational #1)。

仍 defer:冷层类聚 / query 独立 cli / 跨 scope 向量联邦 / BFS_WEIGHT 调参(需 eval)。
