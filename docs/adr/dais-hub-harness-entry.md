# ADR: dais-hub harness 条目接入 (SPECS["dais"])

日期 2026-11-17 · 状态 已实施 (P2-1) · 上游 大编排#2 / 总控起跑令

## 背景

dais-hub (`~/projects/dais-hub`, 七层 headless harness 编排平台) 的
worker 以 `cc-dais` 等形态内跑宿主 harness。memsvc harness 注册表
(`harness.py` SPECS) 需新增 `dais` 条目, 使其 transcript 消费原语
(locate/end_steps/scenes) 可作用于 dais 编排的会话。

## 决策

**薄条目, 同构 openclaw (2026-10-05 先例)**:

| 字段 | 取值 | 依据 |
|---|---|---|
| transcript 定位组 | 静态借 cc adaptor (`_ADAPTORS["dais"] = _ADAPTORS["cc"]`) | dais-hub 无自有 transcript 文件面 (见下) |
| 行 schema | 借 cc (`_SCENES["dais"] = _cc_scenes`) | worker=cc-dais 时 transcript 即 CC 原生 jsonl |
| memory_dir | **None (能力关闭, 响亮报错)** | dais-hub L5 能力面无记忆层; P4 memsvc 整合阶段再议 `~/.dais/projects/<enc>/memory` 约定 |
| spool | `spool_env=None` + 缺省池 `_svc_spool()` (CC 共用池) | dais 无 hook 写端 env; 与 openclaw 取法一致 |
| session_env | None | ADR-0002 全量环境继承 — 宿主 harness env 透传, cc-dais worker 自带 `CLAUDE_CODE_SESSION_ID` |
| corpus_key | 回落 "cc" (`_mk` 缺省: dais 不在 HARNESS_RULES 主键) | wire 同 CC 格式 |
| resolve_harness | 白名单显式放行 "dais" (与 dsh/openclaw 并列) | cli 投影面可用; hooks 场景不遇 (bash case 不动) |

## 四通道数据面事实 (决策地基)

dais-hub L2 四通道中**唯 jsonl 通道有持久文件**, 且文件为**宿主 harness
原生 jsonl** (dais 只 tail 消费, 不构成数据源):

| 通道 | 产物 | memsvc 契约判定 |
|---|---|---|
| jsonl | 宿主原生 jsonl 文件 | **可直投, 零转换** — memsvc 绕过 dais 直扫宿主文件 |
| proxy | hub 进程内 NormalizedEvent 流 (决策流全集), 无文件面 | 需 dais-hub 先加 jsonl 导出面 + 新 adaptor; 增量限 usage/system_prompt, P4 议题 |
| pty | 原始字节流 (xterm 回放), 无行结构 | 不可直投; 等价信息宿主 jsonl 已有, 增量限用户键入/审批交互, 低优先 |
| hook | hub-cli unix socket 规范化事件 | **对账防重定位, 非 ingest 数据源** (见下) |

## 已知张力: 多宿主静态借用 (挂账, P2-2/4 再议)

总控裁 (P2-1): 本段实现**静态借 cc**。但 dais worker harness ∈
{omp-dais, cc-dais, pi-dais} — 非 CC 宿主的 transcript (omp/pi 布局)
本条目**覆盖不到**。分派预案 (P2-2/4 真跑多宿主时启用):

- 方案 canParse: 按转录文件路径形状动态分派宿主 adaptor (dais-hub
  `channels/transcript/adapter.ts` 的 `canParse(path)` 同思路 — 路径
  前缀/文件名模式区分 cc/pi/omp 布局)。
- 该方案与 `_mk` 静态表 (`_ADAPTORS[name]` 单值) 存在结构张力, 届时
  需在 `_ADAPTORS` 值结构或 `cwd_filter` 钩子上扩展, 不在本段预铺。

## hook 通道对账防重 (挂账)

dais-hub 注入的 hub-cli hook (ADR-0006) 与宿主 settings.json 直挂的
memsvc hook 面可能**双写同事件** (同 PreToolUse/PostToolUse)。防重倚靠
现机制: `fact.source_refs` 去重 (sessionId/leafUuid)。**未验证项**:
dais 注入路径下 session 标识是否与宿主原生一致 — P2-2 实跑时对账,
不一致则 ingest 会双份。hook 通道不作为 memsvc 数据源。

## 影响

- `transcripts.py`: `_ADAPTORS["dais"] = _ADAPTORS["cc"]` +
  `_SCENES["dais"] = _cc_scenes` + HARNESSES 增项 (三表名单相等由
  `tests/test_harness_registry.py` 钉死)
- `corpus_prep.py`: HARNESS_RULES 增 `"dais": CC_RULES`
- `harness.py`: SPECS 增 dais 条目 + resolve_harness 白名单增 "dais"
- hooks/session-start-mem.sh bash case **不动** (dais 仅 cli 面, hooks
  场景不遇 — 与 resolve_harness docstring 口径一致)
