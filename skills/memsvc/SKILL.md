---
name: memsvc
description: 手动操作 memory-service 知识图谱（KG）——召回/入库/导入/投影/写事实。用户要"查记忆""记一下这个""导入 memory""刷新投影""补最近会话入库"或任何涉及 memsvc 记忆库的操作时使用。CLI 位于 /home/yy/projects/memory-service/cli.py，db/路径全部模块相对，任意 cwd 可用。接入 cc/dsh/pi 三 harness（omp 暂搁置：内建 mnemopi 记忆语义冲突未裁决）。
---

# memsvc 手动操作

**背景** (2026-08-27 定稿 + M18 增补 + 09-01 终裁A方案): 自动面 = **PreCompact 钩子**（compact 时蒸馏 assistant 每轮输出的 end step 入 KG，`endsteps.py` 过滤）+ **SessionStart 钩子**（synthesis-index 单点自动投影）+ **UserPromptSubmit 钩子**（召回注入）；**consolidation 手动走本 skill**。CC automemory 投影已恢复（09-01 终裁A：08-27「不动」红线取消，SessionStart 单点写 MEMORY.md 投影索引，ADR-A 原生格式）；`recall --project` 召回日志投影照旧（用户明示裁决 2026-08-27）。
库: `data/memory.db`（~1900 active fact / 2300+ entity，SQLite WAL，词法召回毫秒级）。

## 意图 → 命令

| 意图 | 命令 |
|---|---|
| 查记忆/召回 | `python3 /home/yy/projects/memory-service/cli.py recall "<query>" --json --top-k 8` |
| 查记忆 + 落盘到当日 recall 日志 | 同上 + `--project`（正文→`memory/recall-<DATE>.md`，MEMORY.md 注入索引行；dir=cc_memory_dir(`--cwd` 或 `$PWD`)；空命中不投影） |
| 召回（向量融合，解字面盲区） | 同上 + `--vector`（**需 LM Studio 127.0.0.1:16666 在线**） |
| 召回（图近字面远） | 同上 + `--bfs` |
| **补最近会话结论入库**（当前项目最近 N 个 transcript 的场景蒸馏走 LLM） | `python3 …/cli.py ingest-recent [--cwd <项目目录>] [--harness cc\|dsh\|pi\|omp\|codex] [--limit 10]`；先 `--dry-run` 预览（零 LLM 零写入，details 报 scenes/user_blocks 计数）。**M21 用户声音通道**：每个 end step 配对其前累积的用户原话块（≤4 块/1200 字，`MEM_USERVOICE_*` 可调），合成 transcript 带 `[用户]`/`[助手结论]` 角色标记（prompt v5 阅读优先级依赖）。harness 判定：cc=`stop_reason=end_turn`，dsh=`turn/end(completed)` 前最后一条 assistant text（zstd 自动解压；真人判定按 `source.kind=user` 结构过滤 + DSHMSG 信箱载荷剔除），pi/omp=`stopReason=stop`，**codex**=`response_item` assistant `output_text`（用户语料取 `event_msg/user_message` 镜像层天然滤注入；无项目目录，按会话头 `session_meta.cwd` 结构匹配——**必须传 `--cwd`**，不传匹配 `$PWD`）。**omp 暂搁置**（内建 mnemopi 记忆语义冲突未裁决，用户 2026-08-27 裁决先不接） |
| 单个 transcript 入库 | `python3 …/cli.py autodream --session <id> --transcript <path.jsonl> [--cwd <项目目录>] [--harness cc\|codex\|dsh\|pi\|omp]`（可先 `endsteps.py --scenes <t.jsonl> > /tmp/e.jsonl` 场景蒸馏，缺省纯 end step） |
| 导入 memory 目录 | `python3 …/cli.py init-memory --memory-dir <dir> [--cwd <项目目录>]` |
| 单 md 重灌（编辑后） | `python3 …/cli.py re-ingest <file.md> [--cwd <项目目录>]` |
| md 删除同步 | `python3 …/cli.py prune --scope <cwd> --dry-run`（先 dry-run 预览） |
| 刷新投影（MEMORY.md 对账） | `python3 …/cli.py synthesis-index --scope <cwd>` |
| 直接写一条事实 | `python3 …/cli.py write "<subject>" "<predicate>" "<value>" [--fact-type stable\|permanent\|ephemeral]` |
| 证实/失效/晋升/引用 | `confirm\|invalidate\|elevate\|cite <fact_id>`（invalidate 可加 `--note`，cite 可加 `--ref`） |
| 库况 | `python3 …/cli.py stats-json` |
| **看图谱实时生长** | `python3 …/cli.py graph-live --port 8766`（**必须 --port 8766**：默认 8765 被 rt_gateway 语音面板占用；前台阻塞 Ctrl-C 退，无状态秒起。inotify 盯 wal 零轮询，入库即推；页面 http://127.0.0.1:8766/） |
| 导出图谱给外部工具 | `python3 …/cli.py graph-export --json <path>`（快照含 rowid 游标）或 `--csv <dir>`（nodes.csv+edges.csv，边带 created_at 时间列 → **Cosmograph** 时间轴回放原生可识别；Gephi Lite 同吃） |

`…` = `/home/yy/projects/memory-service`（下同）。

## 依赖门（调用前自查）

- **词法召回零依赖**（默认路径，离线可用）；`--vector`/`--bfs` 才需要 LM Studio `127.0.0.1:16666`（qwen3-embedding-4b）。
- **入库类**（ingest-recent / autodream / init-memory / re-ingest）走 LLM 直抽（glm-5-turbo，`.env` 里 `ZHIPU_API_KEY`）——**LLM 不可达即响亮跳过该段，绝不回落 regex**。速度预期 ~12–60s/段；ingest-recent 10 文件 × 多段可能要几十分钟，建议 nohup。
- **语料预处理 (corpus_prep, 2026-08-28)**：喂提取器前按 harness 映射表清洗（cc/codex/dsh/pi 各有 DROP/UNWRAP 规则：system-reminder、AGENTS.md 投影、桥信封、压缩重注入等注入块剥除）+ 密钥脱敏（`redact_secrets` 8 类，LLM 调用前终防线）。接缝三道幂等：`transcripts` 蒸馏口 / `autodream._read_transcript` 逐块 / `llm_extract.extract` 脱敏。**白名单制**——cc 语料 90% 尖括号是代码回显泛型，新增规则必须对真实语料验证防误杀。
- 幂等：autodream/init-memory 重跑按 fact 级 NOOP 去重，安全但**重抽仍花 LLM 时间**——别为单文件重跑全目录，用 re-ingest 单文件。ingest-recent 另有 **sha256 注册表**（`data/transcript-registry.json`）：同文件未变 → 二跑直接 skip 不烧 LLM；transcript 变更 → 自动重跑。

## 陷阱（实测在案）

1. **init-memory 默认目录不是全局 memory**：默认 = `cc_memory_dir(cwd)`（当前目录对应的项目 memory）。要导全局 `~/.claude/projects/-home-yy--claude/memory/` 必须**显式** `--memory-dir`。
2. `--cwd` 语义按子命令不同：recall 是**过滤**（只看该 cwd + NULL 老数据），autodream/init-memory/re-ingest/ingest-recent 是**标记** source_cwd。导入个人全局记忆时不传 `--cwd`（记 NULL=全局）。
3. recall 加 `--cwd` 前先想清楚：现库绝大部分 fact source_cwd=NULL（全局），按 cwd 过滤会漏。
4. `--json` 输出稳定契约（字段名即 ABI），脚本消费必加；人读可不加。`--project` 的报告走 stderr，不污染 stdout 契约。
5. ingest-recent 定位目录按 harness 不同：cc=`~/.claude/projects/<enc>/`（`/`和`.`→`-`）、dsh/pi=`~/.dsh|~/.pi/agent/sessions/-<enc>--/`（`/`→`-`，点保留）、omp=`~/.omp/agent/sessions/<home相对enc>/`。找 transcript 前先 `ls` 确认目录存在，别拿不存在的路径空跑。
6. ingest-recent 与 PreCompact spool 的注册表**不共享**：已被 PreCompact 处理过的会话手动再跑会重复蒸馏一遍（KG fact 层面幂等吸收，多为 noop，但花 LLM 时间）。
7. recall-<DATE>.md 是 mem-service 产物（frontmatter `source: mem-service-recall`），init-memory/re-ingest 扫描会自动跳过（ADR-16f 防自指循环）——不要手动把它灌进 KG。
8. **召回出端打标（2026-08-28 闭环）**：所有召回注入/投影内容整体包 `<memsvc-recall>…</memsvc-recall>` 标记块——两个面：UserPromptSubmit 钩子 `recall_inject.py` 的 additionalContext、`recall --project` 的 recall-<DATE>.md 正文节（MEMORY.md 索引行不打标）。标记是 memsvc 自有中性语法，harness 解析器原样透传（**零适配器**），LLM 读到即知是召回内容；语料重进时 corpus_prep COMMON 规则（五 harness 各表之首）整块丢弃——召回回声不重入库。
9. **实时图 (M20) 语义边界**：`graph-live` 只跟踪 **INSERT 生长**（LIF 衰减/状态流转不推）；页面上删除不反映，要全量态**刷新页面**即可（重新快照）。快照/增量只画 degree>0 实体（孤儿与纯字面事实不成图）；增量对新边端点做**并集补发**（老实体从未下发过、新边连上时必须补，否则悬空）。服务器同源无 CORS；SSE 断线自动重连并按游标补拉错过的增量。
10. **跨 harness 现状**（用户裁决 2026-08-27：接 cc/dsh/pi，omp 搁置）：
   - skill 入口：cc=`~/.claude/skills/memsvc`、dsh=`~/.dsh/skills/memsvc`（watcher 热加载）、pi **零安装**（pi 的 claude 兼容发现层直接扫 `~/.claude/skills`）——三处全是 symlink 指向 repo 正本，单一源。
   - 进端：`ingest-recent --harness cc|dsh|pi` 全通（omp 适配器代码保留但搁置）。
   - 出端：`recall --json` 任何 harness 裸调即可；文件投影（`--project`）只有 CC 布局。dsh/pi 的等价面是 `APPEND_SYSTEM.md` 约定（全局系统提示追加、用户自有文件），接不接待裁决，别擅自动。
   - dsh 有 CC 钩子桥（`dsh-hooks-claude-code`：SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/Stop/SubagentStop）但**无 PreCompact 缝**——CC 的蒸馏钩子骑不上桥，dsh 进端维持手动。
   - omp 侧内建记忆 mnemopi（`retain` 工具 + `/memory` 命令族 + 每 turn 自动 retain）与本服务语义重叠，未裁决前不在 omp 里主动引导使用本 skill。

## 示例

```bash
# 查: 这项目用什么向量索引?
python3 /home/yy/projects/memory-service/cli.py recall "sqlite-vec 向量索引" --json --top-k 5

# 查 + 当日召回日志落盘 (recall-20260827.md + MEMORY.md 索引行)
python3 /home/yy/projects/memory-service/cli.py recall "omp zhipu 凭据" --json --top-k 5 --project

# 补: 当前项目最近 10 个会话的结论入库 (先预览再真跑)
python3 /home/yy/projects/memory-service/cli.py ingest-recent --dry-run
python3 /home/yy/projects/memory-service/cli.py ingest-recent --limit 10

# 记: 一条永久事实
python3 /home/yy/projects/memory-service/cli.py write "memsvc" "uses" "sqlite-vec 作向量索引" --fact-type permanent

# 导: 全局 CC memory 增量入库 (幂等, 只花新文件的钱)
python3 /home/yy/projects/memory-service/cli.py init-memory --memory-dir ~/.claude/projects/-home-yy--claude/memory/

# 刷: 重写当前项目的 MEMORY.md 投影
python3 /home/yy/projects/memory-service/cli.py synthesis-index --scope "$PWD"
```

## 红线

- **绝不 regex 回退**：LLM 断供时入库命令报错/跳段是正确行为，不要绕。
- 生产 `data/` 写入面 = 显式命令 + PreCompact 蒸馏入库（唯一自动面）；其余全手动。
- **投影面（09-01 终裁A方案）**：SessionStart synthesis-index 单点写 CC memory 目录（MEMORY.md 投影索引 + mem-*.md 载体，ADR-A 原生格式，索引行无 `[mem]` 字面标记）+ `recall --project` 日志投影；两族之外不写 CC memory 目录。不做 T1 lazy reconcile（注入面保持只读+LIF 记账）。
- 活钩子 = PreCompact（入库）+ SessionStart（投影）+ UserPromptSubmit（召回注入）；post-tool-use 仍休眠；consolidation 手动。
