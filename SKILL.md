---
name: mem
description: "mem-service 记忆接入:把对话事实/实体落 KG fact 层 + 按需召回相关 Fact。叠加在 CC 现有记忆之上(不改 MEMORY.md)。触发:记忆/recall/ingest/查历史事实/落记忆/结构化记忆/mem service。"
---

# mem — mem-service KG 记忆接入(叠加层)

mem-service 是独立 Python 服务,在 CC 现有记忆(~/.claude memory md,即时派热/温层)之下**叠加**一层 KG fact(Entity + Fact reified + 正交元数据)。**绝不改 CC memory md**——skill 是唯一桥梁,通过 cli 读写服务自己的 SQLite KG [ADR-7]。

v1 形态: **CC 按需调 cli**(用户手动触发,或你读完本 skill 后主动调),无三频 hook 自动注入(per-turn 连续性 defer, Spec Defer)。

> **⚠ 本 SKILL.md 部分描述为 v1(正则抽取/字面召回),已过时**。当前能力:LLM 蝴蝶翼抽取 + `α·match+β·centrality+γ·LIF+δ·vec` 召回 + 向量召回 + PreCompact autoDream + 多值共存。**接线/部署详见 [INSTALL.md](INSTALL.md)**,能力详见 `docs/mem-service-iteration-log.md`。

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

服务源在 `/home/yy/projects/memory-service/`(独立项目, cli 在顶层),cli 入口 = `cli.py`。**cli 直接 import 同目录模块(`extractor`/`store`/`db`),不是 Python 包,必须在服务目录下用 `python3 cli.py` 调,不能从别处 import。**

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

**注意** cli 内部 `import extractor`/`import store` 是同目录裸 import,**实际只接受 cwd = 服务目录 或 把服务目录加进 PYTHONPATH**(绝对路径调若 cwd 不对会 import 失败)。最稳的调法:

```bash
cd /home/yy/projects/memory-service && python3 cli.py <subcommand> ...
# 等价: cd ~/.claude/skills/mem && python3 cli.py <subcommand> ...
```

---

## 子命令契约(严格对齐 cli.py)

三个子命令,**无 `query`**(调试用 `recall --verbose` 或 `sqlite3 data/memory.db`(cwd=服务目录), Spec §3 Defer)。

CC 以 `mem` 为调用名(skill 名 = `mem`, 软链到 `~/.claude/skills/mem/`)。即:**面向 CC 的调用 = `mem recall` / `mem ingest` / `mem consolidate`**,底层 = `cli.py <subcmd>`。两种写法等价:

```bash
mem ingest "<text>"                        # CC 调用名(软链 deploy 后)
python3 cli.py ingest "<text>"             # 底层 cli(cwd=服务目录)
```

### 1. `ingest` / `mem ingest` — 抽实体+事实入 KG

```bash
mem ingest "<text>" [--source <ref>]
# 等价: python3 cli.py ingest "<text>" [--source <ref>]
```

- 正则 `EntityExtractor`(7 英文谓词 + 中文同义集 + 9 模式类,**无 LLM**, ADR-5)抽 Entity + Fact
- Fact 标 `extractor="regex"`;实体按 `(name, entity_type)` 去重(重抽复用既有 id)
- `--source`: 可选来源引用(如会话 id / 文件路径),写进 `fact.source_refs`
- stdout: JSON `{"entities": <n>, "facts": [<fid>, ...]}`

**示例**:
```bash
$ python3 cli.py ingest "用户使用 rust 进行开发"
{"entities": 2, "facts": ["a962a25ffe6644eabf56ab1c6457d560"]}
# → Fact(subject=用户, predicate=uses, object=rust, extractor=regex)
```

### 2. `recall` / `mem recall` — KG 导航召回 Fact(match×lif 排序)

```bash
mem recall "<query>" [--verbose]
# 等价: python3 cli.py recall "<query>" [--verbose]
```

- v1 召回 = **子串/前缀匹配**(Spec Defer): `entity.name LIKE %query%` 定位 seed 实体 → 其 subject/object 的 Fact → score = match × Fact.LIF 标量(ADR-4; lif 读 `Fact.LIF` 列,非 NeuralField)
- 无 seed 时回退到 `fact.value/predicate LIKE %query%`
- 结果按 score 降序
- `--verbose`: 每条 Fact 追加 `_scored`/`_subject_name`/`_object_name` 调试字段(替代被 defer 的 query cli)
- stdout: JSON Fact 数组

**示例**:
```bash
$ python3 cli.py recall "rust"
[{"id":"...","subject_id":"...","predicate":"uses","object_id":"...","value":"rust","LIF":0.5,"extractor":"regex","status":"active",...}]

$ python3 cli.py recall "rust" --verbose   # 带 _scored/_subject_name/_object_name
```

**v1 召回边界**: 中文同义/省称/改写 query 命中率低(字面子串匹配);语义召回 defer 到向量实体 tag + 聚合度重排层(P4)。预期场景 6(GIVEN ingest "用户使用 rust..." WHEN recall "rust")字面命中成立;改写为 "rusty" / "Rust 语言" v1 不保证命中。

### 3. `consolidate` / `mem consolidate` — 去重骨架(无衰减)

```bash
mem consolidate
# 等价: python3 cli.py consolidate
```

- 精确重复 Fact(`subject_id, predicate, object_id, value` 全同)标 `status=superseded`,保留最早一条
- **无衰减**(type-aware LIF 衰减 defer, ADR-6;随 autoDream consolidate 阶)
- stdout: JSON `{"superseded": <n>, "active": <n>}`

---

## 闭环示例(Spec §4 story 6)

```bash
$ cd /home/yy/projects/memory-service
$ python3 cli.py ingest "用户使用 rust 进行开发"
{"entities": 2, "facts": ["..."]}
$ python3 cli.py recall "rust"           # → 命中 uses(用户, rust), scored=1.0×LIF
$ python3 cli.py consolidate             # → {"superseded": 0, "active": 1}
```

---

## 数据与状态

- KG 数据: `data/memory.db`(cwd=服务目录; SQLite,服务自治, ADR-7)
- schema: `schema.sql`(Entity + Fact reified,**无 MemoryItem 表**, ADR-2/3)
- 状态独立于 CC memory md;v1 不投影回 md(桥接留后, Spec Defer)

## 何时不该用

- 即时对话上下文(CC memory md 已是即时派热/温层,够用)
- 需要 per-turn 自动注入记忆(三频 hook defer)
- 需要语义/向量召回(P4)
- 需要冷层类聚 / autoDream 后台巩固(后续阶)

---

## 实现边界(v1 defer 项, 见 Spec §3/§9)

**已实现(非 defer)**:LLM 蝴蝶翼抽取(ADR-5b)+ `α·match+β·centrality+γ·LIF+δ·vec` 召回(ADR-4v2)+ 向量召回 `--vector`(ADR-13)+ PreCompact autoDream hook(ADR-10/11)+ type-aware decay(ADR-8v2)+ 多值谓词共存 + KG→CC 投影 `build-index`(ADR-15)。

仍 defer:autoDream daemon(常驻)/ 冷层类聚 / query 独立 cli / 跨 scope 向量联邦。
