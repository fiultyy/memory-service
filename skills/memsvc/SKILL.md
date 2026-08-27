---
name: memsvc
description: 手动操作 memory-service 知识图谱（KG）——召回/入库/导入/投影/写事实。用户要"查记忆""记一下这个""导入 memory""刷新投影""补最近会话入库"或任何涉及 memsvc 记忆库的操作时使用。CLI 位于 /home/yy/projects/memory-service/cli.py，db/路径全部模块相对，任意 cwd 可用。
---

# memsvc 手动操作

**背景** (2026-08-27 定稿 + M18 增补): 唯一自动面 = **PreCompact 钩子**（compact 时蒸馏 assistant 每轮输出的 end step 入 KG，`endsteps.py` 过滤）；**召回与 consolidation 全手动走本 skill**。CC automemory 机制本身不动——例外: `recall --project` 会把**召回结果**投影成 `memory/recall-<DATE>.md` + MEMORY.md 索引行（用户明示裁决 2026-08-27）。
库: `data/memory.db`（~1900 active fact / 2300+ entity，SQLite WAL，词法召回毫秒级）。

## 意图 → 命令

| 意图 | 命令 |
|---|---|
| 查记忆/召回 | `python3 /home/yy/projects/memory-service/cli.py recall "<query>" --json --top-k 8` |
| 查记忆 + 落盘到当日 recall 日志 | 同上 + `--project`（正文→`memory/recall-<DATE>.md`，MEMORY.md 注入索引行；dir=cc_memory_dir(`--cwd` 或 `$PWD`)；空命中不投影） |
| 召回（向量融合，解字面盲区） | 同上 + `--vector`（**需 LM Studio 127.0.0.1:16666 在线**） |
| 召回（图近字面远） | 同上 + `--bfs` |
| **补最近会话结论入库**（当前项目最近 N 个 transcript 的 end step 走 LLM） | `python3 …/cli.py ingest-recent [--cwd <项目目录>] [--harness cc\|dsh\|omp] [--limit 10]`；先 `--dry-run` 预览（零 LLM 零写入）。harness 判定：cc=`stop_reason=end_turn`，dsh=`turn/end(completed)` 前最后一条 assistant text（zstd 自动解压），omp=`stopReason=stop` |
| 单个 transcript 入库 | `python3 …/cli.py autodream --session <id> --transcript <path.jsonl> [--cwd <项目目录>]`（可先 `endsteps.py <t.jsonl> > /tmp/e.jsonl` 蒸馏） |
| 导入 memory 目录 | `python3 …/cli.py init-memory --memory-dir <dir> [--cwd <项目目录>]` |
| 单 md 重灌（编辑后） | `python3 …/cli.py re-ingest <file.md> [--cwd <项目目录>]` |
| md 删除同步 | `python3 …/cli.py prune --scope <cwd> --dry-run`（先 dry-run 预览） |
| 刷新投影（MEMORY.md 对账） | `python3 …/cli.py synthesis-index --scope <cwd>` |
| 直接写一条事实 | `python3 …/cli.py write "<subject>" "<predicate>" "<value>" [--fact-type stable\|permanent\|ephemeral]` |
| 证实/失效/晋升/引用 | `confirm\|invalidate\|elevate\|cite <fact_id>`（invalidate 可加 `--note`，cite 可加 `--ref`） |
| 库况 | `python3 …/cli.py stats-json` |

`…` = `/home/yy/projects/memory-service`（下同）。

## 依赖门（调用前自查）

- **词法召回零依赖**（默认路径，离线可用）；`--vector`/`--bfs` 才需要 LM Studio `127.0.0.1:16666`（qwen3-embedding-4b）。
- **入库类**（ingest-recent / autodream / init-memory / re-ingest）走 LLM 直抽（glm-5-turbo，`.env` 里 `ZHIPU_API_KEY`）——**LLM 不可达即响亮跳过该段，绝不回落 regex**。速度预期 ~12–60s/段；ingest-recent 10 文件 × 多段可能要几十分钟，建议 nohup。
- 幂等：autodream/init-memory 重跑按 fact 级 NOOP 去重，安全但**重抽仍花 LLM 时间**——别为单文件重跑全目录，用 re-ingest 单文件。ingest-recent 另有 **sha256 注册表**（`data/transcript-registry.json`）：同文件未变 → 二跑直接 skip 不烧 LLM；transcript 变更 → 自动重跑。

## 陷阱（实测在案）

1. **init-memory 默认目录不是全局 memory**：默认 = `cc_memory_dir(cwd)`（当前目录对应的项目 memory）。要导全局 `~/.claude/projects/-home-yy--claude/memory/` 必须**显式** `--memory-dir`。
2. `--cwd` 语义按子命令不同：recall 是**过滤**（只看该 cwd + NULL 老数据），autodream/init-memory/re-ingest/ingest-recent 是**标记** source_cwd。导入个人全局记忆时不传 `--cwd`（记 NULL=全局）。
3. recall 加 `--cwd` 前先想清楚：现库绝大部分 fact source_cwd=NULL（全局），按 cwd 过滤会漏。
4. `--json` 输出稳定契约（字段名即 ABI），脚本消费必加；人读可不加。`--project` 的报告走 stderr，不污染 stdout 契约。
5. ingest-recent 定位目录按 harness 不同：cc=`~/.claude/projects/<enc>/`（`/`和`.`→`-`）、dsh=`~/.dsh/sessions/-<enc>--/session-<uuid>/session.jsonl(.zstd)`、omp=`~/.omp/agent/sessions/<home相对enc>/<ts>_<uuid>.jsonl`。找 transcript 前先 `ls` 确认目录存在，别拿不存在的路径空跑。
6. ingest-recent 与 PreCompact spool 的注册表**不共享**：已被 PreCompact 处理过的会话手动再跑会重复蒸馏一遍（KG fact 层面幂等吸收，多为 noop，但花 LLM 时间）。
7. recall-<DATE>.md 是 mem-service 产物（frontmatter `source: mem-service-recall`），init-memory/re-ingest 扫描会自动跳过（ADR-16f 防自指循环）——不要手动把它灌进 KG。
8. **跨 harness 通用性现状**（M19）：进端 `ingest-recent --harness` 已通用（cc/dsh/omp）；出端 `recall --json` 任何能起进程的 harness 都能裸调（绝对路径，db/.env 模块相对）；**文件投影面只有 CC 做了**（`--project`）——dsh/omp 的等价面是 `APPEND_SYSTEM.md` 约定（pi 系全局系统提示追加，每次请求现读），属用户自有文件+全局生效，接不接、注入什么粒度**待用户裁决**，别擅自动。

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
- **CC automemory 不动**：不跑 synthesis-index 写 CC memory 目录（投影已从管道移除），除非用户明确要求。**唯一例外** = `recall --project` 的 recall 日志投影（用户 2026-08-27 明示）。
- 其余三个钩子（session-start/user-prompt-recall/post-tool-use）保持休眠；consolidation 手动。
