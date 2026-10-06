# OpenClaw internals 速查 (2026-10-06 考古固化)

> 对象: `~/tools/openclaw-rebase` (Claw 私有构建)。rebase 后本页结论需重验。
> 定位均为只读考古结论; 配置正本 `~/.openclaw/openclaw.json`。

## 压缩双路径 (T6 缺口根因)

- manual/budget/preflight 全走 `compaction-hooks.ts:253` `runBeforeCompaction` — 事件只传 messageCount/tokenCount, **不带 messages** (trigger=budget 实测 275 条被压零进 KG)。
- 仅流式 auto 走 `embedded-agent-subscribe.handlers.compaction.ts:85` — 带 messages。
- 插件侧对策: memsvc T6 存储 seam fallback (事件无 messages 时直读 agent sqlite)。

## session 存储真身

- 活会话: `~/.openclaw/agents/<id>/agent/openclaw-agent.sqlite` — `transcript_events(session_id, seq, event_json, created_at)` **追加式, 压缩不删事件**, 全历史可读 (node:sqlite readOnly)。
- `sessions/` 目录全是 `.deleted.zst` 死归档; agent 目录下另一同名 sqlite (~610KB) 是旧位置空库, 别读错。
- 事件 JSON: `{"type":"message","message":{"role":...,"content":[...]}}` 行态。

## 配置三层覆盖

`agents.entries.<id>.model` > `agents.defaults.subagents.model` > `agents.defaults.model`。

- 坑: dict-only 探查脚本漏 list (fallbacks 数组) → 曾漏改 5 个 agent。审计用 `scripts/json-find.py`。

## 压缩真闸门 (retired 键别再配)

- threshold = contextWindow(1M) − reserveTokensFloor(**硬编码 20k**) ≈ 980k; `MIN_PROMPT_BUDGET_RATIO=0.5` 把 reserve cap 在 500k — 300k token 阈值架构上配不出。
- `sessions.compaction.reserveTokensFloor` 是 RETIRED 键; 活配置口 = `agents.defaults.compaction.maxActiveTranscriptBytes` (字节闸, 生产 "1mb")。
- 两层压力系统: embedded 预检软线 ~300k (1M−700k toolReserve) 只打 route=compact_only 诊断; 真压缩看 980k / 字节闸。

## 模型路由与 cache

- **Code Mode 断崖**: `isCodeModeEngagedForModel` (auto 按模型 `compat.codeMode==="preferred"`) → tools 64↔66 (`createCodeModeTools` 返回恰 [exec, wait] 两件) + systemPrompt digest 同变 → cache 断崖。根治 = 单 provider 单协议单 key。
- 协议: anthropic 显式 cache_control 断点 (5min TTL) vs openai 隐式前缀。zhipu coding-plan **有** `/api/anthropic` 口 — 49 位 ANTHROPIC_AUTH_TOKEN (28 位 openai 型 key 只通 `/api/coding/paas/v4`)。
- cache drop 日志只打工具**数量**差, 工具名在 snapshot 有但 `diffSnapshots` 不输出 — 追工具面变化读 `prompt-cache-observability.ts` 源码, 别从日志反猜。
- 插件热重载延迟可达 ~30min; `[System]` 前缀 = 内部系统 turn (无用户意图, 整条跳过召回计次)。
