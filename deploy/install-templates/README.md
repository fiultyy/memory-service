# memsvc 安装模板 — 三端接线(cc / dsh / openclaw)

> 路径占位:模板内 `/home/yy/projects/memory-service` = 本 repo 根,安装时替换为你的实际路径。
> dsh hooks 本体 `hooks/dsh-hooks.json` 与三个 shell 钩子(`hooks/pre-compact-mem.sh` 等)已在 repo 内,无需拷贝。

## 1. CC(Claude Code)

把 `cc-settings-hooks.json` 的三个键(PreCompact / SessionStart / UserPromptSubmit)合并进 `~/.claude/settings.json` 的 `hooks` 段(已有的 matcher 段追加 `hooks` 数组元素,勿整段覆盖)。

- 生效:每 session 启动对账投影 / 前 n turn 召回注入 / compact 前快照进 spool
- `MEM_HARNESS=cc` 已写在命令里;三脚本均恒 exit 0 fail-open,timeout 20s

## 2. dsh

`dsh-cordis-patch.snippet.yml` 的 insert 块追加进 `~/.dsh/cordis.patch.yml`(或你 dsh 的 patch 链),引用 repo 内 `hooks/dsh-hooks.json`(MEM_HARNESS=dsh 面的同类钩子注册)。

## 3. OpenClaw

1. `openclaw-extension/` 三件拷到 `~/.openclaw/extensions/memsvc/`
2. **改 `index.js` 顶部三个路径常量**: `CLI` / `SPOOL_DIR` / `OC_ROOT`
3. `openclaw-plugins-entry.snippet.json` 合并进 `~/.openclaw/openclaw.json`(plugins.entries.memsvc)
4. 重启 gateway;验证 `node <openclaw-dist>/index.js plugins inspect memsvc` = loaded

插件行为:
- **T5 注入**:每 session 前 3 个真实 user turn(≥4 字符,剥 `[System]`/Feishu `System:` 行)spawn CLI 召回,`prependContext` 注入(不打断前缀缓存)
- **T6 快照**:before_compaction 时优先用事件 messages;manual/budget 路径事件不带 messages 则**存储 seam fallback**——node:sqlite 只读 `~/.openclaw/agents/<agentId>/agent/openclaw-agent.sqlite` 的 `transcript_events`(追加式全历史),过滤 user/assistant 落 spool,段级 sha 幂等
- 提前压缩可选:`agents.defaults.compaction.maxActiveTranscriptBytes: "1mb"`(字节闸,压缩即回填点)

## 服务端(repo 内,三端共用)

`deploy/systemd-user/memory-ingest.service` — spool 段级消费 + watchdog 双端 md ingest + dreaming + hygiene,`systemctl --user enable --now memory-ingest`。
