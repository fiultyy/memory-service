---
name: memsvc
description: 手动操作 memory-service 知识图谱（KG）——召回/入库/导入/投影/写事实。用户要"查记忆""记一下这个""导入 memory""刷新投影"或任何涉及 memsvc 记忆库的操作时使用。CLI 位于 /home/yy/projects/memory-service/cli.py，db/路径全部模块相对，任意 cwd 可用。
---

# memsvc 手动操作

**背景** (2026-08-27 定稿): 唯一自动面 = **PreCompact 钩子**（compact 时蒸馏 assistant 每轮输出的 end step 入 KG，`endsteps.py` 过滤）；**召回与 consolidation 全手动走本 skill**；CC automemory 机制不动（KG 不写 CC memory 目录，无投影）。
库: `data/memory.db`（~1900 active fact / 2300+ entity，SQLite WAL，词法召回毫秒级）。
手动补历史会话结论入库: `python3 /home/yy/projects/memory-service/endsteps.py <transcript.jsonl> > /tmp/e.jsonl && python3 …/cli.py autodream --session <id> --transcript /tmp/e.jsonl --cwd <项目目录>`。

## 意图 → 命令

| 意图 | 命令 |
|---|---|
| 查记忆/召回 | `python3 /home/yy/projects/memory-service/cli.py recall "<query>" --json --top-k 8` |
| 召回（向量融合，解字面盲区） | 同上 + `--vector`（**需 LM Studio 127.0.0.1:16666 在线**） |
| 召回（图近字面远） | 同上 + `--bfs` |
| 会话 transcript 入库 | `python3 …/cli.py autodream --session <id> --transcript <path.jsonl> [--cwd <项目目录>]` |
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
- **入库类**（autodream / init-memory / re-ingest）走 LLM 直抽（glm-5-turbo，`.env` 里 `ZHIPU_API_KEY`）——**LLM 不可达即响亮跳过该段，绝不回落 regex**。速度预期 ~12–60s/段；init-memory 一个 24 文件目录 ≈ 35min。
- 幂等：autodream/init-memory 重跑按 fact 级 NOOP 去重，安全但**重抽仍花 LLM 时间**——别为单文件重跑全目录，用 re-ingest 单文件。

## 陷阱（实测在案）

1. **init-memory 默认目录不是全局 memory**：默认 = `cc_memory_dir(cwd)`（当前目录对应的项目 memory）。要导全局 `~/.claude/projects/-home-yy--claude/memory/` 必须**显式** `--memory-dir`。
2. `--cwd` 语义按子命令不同：recall 是**过滤**（只看该 cwd + NULL 老数据），autodream/init-memory/re-ingest 是**标记** source_cwd。导入个人全局记忆时不传 `--cwd`（记 NULL=全局）。
3. recall 加 `--cwd` 前先想清楚：现库绝大部分 fact source_cwd=NULL（全局），按 cwd 过滤会漏。
4. `--json` 输出稳定契约（字段名即 ABI），脚本消费必加；人读可不加。

## 示例

```bash
# 查: 这项目用什么向量索引?
python3 /home/yy/projects/memory-service/cli.py recall "sqlite-vec 向量索引" --json --top-k 5

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
- **CC automemory 不动**：不跑 synthesis-index 写 CC memory 目录（投影已从管道移除），除非用户明确要求。
- 其余三个钩子（session-start/user-prompt-recall/post-tool-use）保持休眠；召回、consolidation 手动。
