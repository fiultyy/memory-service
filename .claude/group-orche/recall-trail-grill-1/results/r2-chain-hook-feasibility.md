# Round 2 终稿:UserPromptSubmit hook 恢复可行性

## 【查证】
- `settings.json:140-149`:UserPromptSubmit 已注册但只调 Orca 通用 hook(零副作用 exit 0)。
- `CLAUDE.md:24`:标注"UserPromptSubmit 等仍禁用",归档 `.archive/hooks-disabled-2026-06-17/`。
- 原始 `user_prompt_submit.py`:写 sessions/{session_id}.json + 调 LLM 生成 agent_name + 可选 validate(exit 2 阻断)。
- 归档时间戳 2026-03-16(统一禁用,原因未记录,但 LLM 调用/validate 阻断是风险)。
- 现役 PreCompact hook 模式:零副作用、exit 0 放行、tolerate 一切。

## 【结论】否决 UserPromptSubmit hook 方案
1. 恢复风险高(原脚本 LLM 调用慢/失败/validate 阻断;2026-03 统一禁用有因)。
2. 写 session_id 不必要——CC 已在 hook stdin 提供 session_id,且 **env 方案更优**(见 chain-alt-mechanism)。
3. UserPromptSubmit 高频(每 prompt),写 session_id 过重。

## 【修订方案】放弃 UserPromptSubmit。最优 = CLAUDE_CODE_SESSION_ID env(chain-alt-mechanism 证实)。备选 SubagentStart 轻量 hook 被 chain-concurrency 否决(文件方案并发不可行)。**env 胜出。**
