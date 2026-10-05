---
name: memsvc
description: 手动操作 memory-service 知识图谱（KG）——召回/入库/导入/投影/写事实。用户要"查记忆""记一下这个""导入 memory""刷新投影""补最近会话入库"或任何涉及 memsvc 记忆库的操作时使用。CLI 位于 /home/yy/projects/memory-service/cli.py，db/路径全部模块相对，任意 cwd 可用。自动面已接 cc/dsh 两 harness（pi 手动面通，omp 搁置）。
---

# memsvc 手动操作

**v4 终态背景** (2026-10-04 解耦后): 自动面 = **三钩子 + mem_daemon 常驻**:
- **PreCompact**（cc/dsh 两面）→ 快照 transcript 进 spool → **mem_daemon 段级消费**: 切段 = `[用户] 用户文本 ≤1200字 + [助手] end_turn 全文` 配对（tool_use/tool_result/thinking/侧链/纯用户尾部跳过）→ **四色裁决**: jev 1.13 缝扫描切语义段（openrouter 云端）→ zhipu 一段一判（七类标签 + gist ≤80 字精炼）→ LMStudio 向量 → 判官 p_dur 审计 → atom 入图（`text`=段原文逐字, `gist`=精炼结论）;
- **SessionStart** → synthesis-index 对账投影（MEMORY.md 单点重写）;
- **UserPromptSubmit** → 首 n turn 召回注入（`<memsvc-recall>` 包裹）;
- **daemon**（systemd `memory-ingest`）另跑夜间 dreaming（settle/supersede/TTL 21d/tag 聚类挂树/audit 挂载）+ hygiene。

**防重三层**: spool 文件 offset 水位 → 段水位（`chunk-seg:` sha，跨快照重放零 LLM）→ chunk sha（零重复入图）。PreCompact 重放安全。

库: `data/memory.db`（~1400 live atoms：段级 954 + 句级 445 遗留；11526 实体；599 语义 tag；SQLite WAL，词法召回毫秒级）。

**解耦架构** (2026-10-04): hooks/*.sh 守护壳 → `runtime.py`（SDK: snapshot/project/inject）→ `harness.py` SPECS（五家 harness 注册表）→ 核心层。agent 手动面只依赖 cli.py 22 子命令（签名零改动）——任意 harness 能起进程即可裸调。

## 意图 → 命令

`…` = `/home/yy/projects/memory-service`（下同）。

| 意图 | 命令 |
|---|---|
| 查记忆/召回 | `python3 …/cli.py recall "<query>" --json --top-k 8` |
| 查记忆 + 落盘到当日 recall 日志 | 同上 + `--project`（正文→`memory/recall-<DATE>.md`，MEMORY.md 注入索引行；空命中不投影） |
| 召回（向量融合，解字面盲区） | 同上 + `--vector`（**需 LM Studio 127.0.0.1:16666 在线**） |
| 召回（图近字面远） | 同上 + `--bfs` |
| 点时召回（历史态） | 同上 + `--as-of <ISO>`（bi-temporal，只返回该时刻有效 fact） |
| **补最近会话结论入库** | `python3 …/cli.py ingest-recent [--cwd <项目目录>] [--harness cc\|dsh\|pi\|omp\|codex] [--limit 10]`；先 `--dry-run` 预览（零 LLM 零写入）。**M21 用户声音通道**: 场景 = end step 配对前累积用户原话块（≤1200 字，`[用户]`/`[助手结论]` 角色标记）。harness 判定: cc=`stop_reason=end_turn`、dsh=`turn/end(completed)`（zstd 自动解压 + source.kind 真人过滤）、pi/omp=`stopReason=stop`、codex=`response_item` assistant `output_text`（无项目目录，按 `session_meta.cwd` 匹配——**必须传 `--cwd`**）。**omp 暂搁置**（内建 mnemopi 语义冲突未裁决） |
| 单个 transcript 入库 | `python3 …/cli.py autodream --session <id> --transcript <path.jsonl> [--cwd <项目目录>] [--harness cc\|codex\|dsh\|pi\|omp]` |
| 导入 memory 目录 | `python3 …/cli.py init-memory --memory-dir <dir> [--cwd <项目目录>]` |
| 单 md 重灌（编辑后） | `python3 …/cli.py re-ingest <file.md> [--cwd <项目目录>]` |
| md 删除同步 | `python3 …/cli.py prune --scope <cwd> --dry-run`（先 dry-run 预览） |
| 刷新投影（MEMORY.md 对账） | `python3 …/cli.py synthesis-index --scope <cwd>`（dsh 面 `--harness dsh` → 落 `~/.dsh/projects/<enc>/memory`） |
| 直接写一条事实 | `python3 …/cli.py write "<subject>" "<predicate>" "<value>" [--fact-type stable\|permanent\|ephemeral]` |
| 证实/失效/晋升/引用 | `confirm\|invalidate\|elevate\|cite <fact_id>`（invalidate 可加 `--note`，cite 可加 `--ref`） |
| 库况 | `python3 …/cli.py stats-json` |
| **看图谱实时生长** | `python3 …/cli.py graph-live --port 8766`（**必须 --port 8766**：8765 被 rt_gateway 占用；前台阻塞 Ctrl-C 退，无状态秒起。inotify 盯 wal，入库即推） |
| 导出图谱 | `python3 …/cli.py graph-export --json <path>` 或 `--csv <dir>`（nodes.csv+edges.csv 带 created_at → Cosmograph 时间轴回放） |

## 依赖门（调用前自查）

- **词法召回零依赖**（默认路径，离线可用）；`--vector`/`--bfs` 才需要 LM Studio `127.0.0.1:16666`（text-embedding-qwen3-embedding-4b）。
- **入库类**（ingest-recent / autodream / init-memory / re-ingest）走 LLM 直抽（glm-5-turbo，`.env` `ZHIPU_API_KEY`）——**LLM 不可达即响亮跳过该段，绝不回落 regex**。速度预期 ~12–60s/段；ingest-recent 10 文件 × 多段可能要几十分钟，建议 nohup。
- **jev 判官**（gate/审计面）走 `.env` `MEM_LAYA_BACKEND`（当前 openrouter 云端 jev 1.13；本地降级备份 jevstyle 容器）——判官不可达时 daemon 段挂起重试（不裸入图），手动 recall gate 面同理。
- **语料预处理 (corpus_prep)**: 喂提取器前按 harness 映射表清洗（五家各有 DROP/UNWRAP 规则）+ 密钥脱敏（`redact_secrets` 8 类，LLM 调用前终防线）。接缝三道幂等: `transcripts` 蒸馏口 / `autodream._read_transcript` / `llm_extract.extract`。白名单制，新增规则必须对真实语料验证。
- 幂等: autodream/init-memory 重跑按 fact 级 NOOP 去重，安全但**重抽仍花 LLM 时间**——别为单文件重跑全目录，用 re-ingest 单文件。ingest-recent 另有 **sha256 注册表**（`data/transcript-registry.json`）: 同文件未变 → 二跑 skip 不烧 LLM。

## 陷阱（实测在案）

1. **init-memory 默认目录不是全局 memory**: 默认 = `cc_memory_dir(cwd)`。要导全局 `~/.claude/projects/-home-yy--claude/memory/` 必须**显式** `--memory-dir`。
2. `--cwd` 语义按子命令不同: recall 是**过滤**（只看该 cwd + NULL 老数据），autodream/init-memory/re-ingest/ingest-recent 是**标记** source_cwd。导入个人全局记忆时不传 `--cwd`（记 NULL=全局）。
3. recall 加 `--cwd` 前先想清楚: 现库绝大部分 fact source_cwd=NULL（全局），按 cwd 过滤会漏。
4. `--json` 输出稳定契约（字段名即 ABI），脚本消费必加；人读可不加。`--project` 的报告走 stderr，不污染 stdout 契约。
5. ingest-recent 定位目录按 harness 不同: cc=`~/.claude/projects/<enc>/`（`/`和`.`→`-`）、dsh/pi=`~/.dsh|~/.pi/agent/sessions/-<enc>--/`、omp=`~/.omp/agent/sessions/<home相对enc>/`。找 transcript 前先 `ls` 确认目录存在。
6. **手动 ingest-recent 与 daemon spool 车道独立**: 各自注册表/水位不共享——daemon 消费过的会话手动再跑会重新蒸馏一遍（KG 幂等吸收，但花 LLM 时间；且两车道产物口径不同: daemon=段级 atom 带 gist, 手动=scenes 蒸馏）。补跑前先 `stats-json` 看 atom 是否已在。
7. recall-<DATE>.md 是 mem-service 产物（frontmatter `source: mem-service-recall`），init-memory/re-ingest 扫描自动跳过（ADR-16f 防自指）——不要手动灌进 KG。
8. **召回出端打标**: 所有召回注入/投影内容整体包 `<memsvc-recall>…</memsvc-recall>`——memsvc 自有中性语法，harness 解析器原样透传（**零适配器**）；语料重进时 corpus_prep 整块丢弃，召回回声不重入库。
9. **实时图 (M20) 语义边界**: `graph-live` 只跟踪 INSERT 生长（衰减/软删不推），要全量态刷新页面。快照/增量只画 degree>0 实体；SSE 断线自动重连按游标补拉。
10. **跨 harness 现状** (2026-10-04 解耦后):
    - skill 入口: cc=`~/.claude/skills/memsvc`、dsh=`~/.dsh/skills/memsvc`（watcher 热加载）、pi 零安装（pi 的 claude 兼容发现层直接扫 `~/.claude/skills`）——三处全 symlink 指向 repo 正本 `~/projects/memory-service/skills/memsvc`，单一源。
    - **自动面**: cc/dsh 两家三钩子全通（dsh 经 `dsh-hooks-claude-code` 桥; PreCompact 有缝——payload 空 transcript_path 时按 session_id 回查 `~/.dsh/sessions`，zstd 明文化 + cid12 幂等键 + `.harness` sidecar 判型）。
    - **出端投影**: cc 布局 + dsh 布局（`~/.dsh/projects/<enc>/memory`，SessionStart `MEM_HARNESS=dsh` 自动）；pi 无投影约定（SPECS `memory_dir=None` 能力关闭，调用响亮报错）。
    - **接新 harness 路径**: CC 形 payload+transcript ≈ SPECS 加条目 10 行；自有格式 ≈ transcripts 加 walker + SPECS 条目（模板即 `_dsh` 系列）。
    - **openclaw (2026-10-05 适配)**: 装点 `~/.openclaw/workspace*/skills/memsvc` symlink 同一正本。**不走 hooks**——ingest 由 daemon watchdog（`src/openclaw_watch.py`）轮询 `~/.openclaw/workspace*/memory/*.md`（topics 一文件一事实带 frontmatter，description 即 gist）；文件 sha 水位 + 段级 distill_seen 双层去重，文件重组后变段自动走 cov 消融。召回直接 `recall` 子命令（词法/向量/BFS 全可用，harness 无关）。
    - omp 侧内建记忆 mnemopi 与本服务语义重叠，未裁决前不在 omp 里主动引导使用本 skill。

## 示例

```bash
# 查: 这项目用什么向量索引?
python3 /home/yy/projects/memory-service/cli.py recall "sqlite-vec 向量索引" --json --top-k 5

# 查 + 当日召回日志落盘 (recall-20261004.md + MEMORY.md 索引行)
python3 /home/yy/projects/memory-service/cli.py recall "omp zhipu 凭据" --json --top-k 5 --project

# 补: 当前项目最近 10 个会话的结论入库 (先预览再真跑)
python3 /home/yy/projects/memory-service/cli.py ingest-recent --dry-run
python3 /home/yy/projects/memory-service/cli.py ingest-recent --limit 10

# 记: 一条永久事实
python3 /home/yy/projects/memory-service/cli.py write "memsvc" "uses" "sqlite-vec 作向量索引" --fact-type permanent

# 导: 全局 CC memory 增量入库 (幂等, 只花新文件的钱)
python3 /home/yy/projects/memory-service/cli.py init-memory --memory-dir ~/.claude/projects/-home-yy--claude/memory/

# 刷: 重写当前项目的 MEMORY.md 投影 (dsh 面加 --harness dsh)
python3 /home/yy/projects/memory-service/cli.py synthesis-index --scope "$PWD"
```

## 红线

- **绝不 regex 回退**: LLM 断供时入库命令报错/跳段是正确行为，不要绕。
- 生产 `data/` 写入面 = 显式命令 + daemon 自动车道（PreCompact spool 段级消费 + 夜间 dreaming + hygiene）；其余全手动。
- **投影面**: SessionStart synthesis-index 单点写 memory 目录（MEMORY.md 投影索引 + mem-*.md 载体，ADR-A 原生格式）+ `recall --project` 日志投影；两族之外不写 CC memory 目录。注入面只读+LIF 记账（互斥防双写: 进上下文/写散件永不同开）。
- **活钩子** = PreCompact（入库）+ SessionStart（投影）+ UserPromptSubmit（召回注入）+ mem_daemon（常驻: 消费/梦/卫生）; consolidation **已自动化**（settle/TTL/tag dream/audit），手动票仅作干预入口。
- 判官断供 → daemon 段挂起持有待恢复（H0 不裸入图）；hook 一切失败路径恒 exit 0（增强面绝不阻塞事件）。
