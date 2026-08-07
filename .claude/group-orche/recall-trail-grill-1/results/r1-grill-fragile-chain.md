# Grill 终稿:session_id 传递链脆弱性

## 【查证】
- ADR-16a 要求 cli recall 加 --session,但当前 `cli.py:239-245` recall 子命令无 --session;`cli.py:287` 调用未传 session_id。Node B T_B1 是待实现任务。
- CC agent 调 recall 实际路径:`mem recall "rust"`,无 --session 可选;实现后忘带也无报警(ADR-16 Consequences:未带则形式1静默失效)。
- transcript 反查:`autodream.py:37-73` _read_transcript 只取 user/assistant 的 text block,跳过 tool_result(line 60,69)→ recall 的 JSON 工具结果无法反查。ADR-16 Alternatives 已正确驳回此方案。
- seen_sessions 反推:`scoring.py:370-373` 是 fact 级 JSON 数组;可 `seen_sessions LIKE '%<sid>%'` 反查本 session 召回过的 fact。**但不是兜底**:若 recall 未带 session_id,refresh_lif_on_recall 中 session_id=None,seen_sessions 不累积 → 查询空集。
- 失效概率:**高**。无强制机制、无 hook 注入、未带静默失效无报错。

## 【结论】加 hook 兜底
接受脆弱面不可行(形式1 是 ADR-16 核心价值,依赖自觉则形同虚设);改设计(trail 文件)成本高已驳回;加 hook 兜底可行:恢复 UserPromptSubmit hook 写 session_id 到临时文件,cli 读作 --session 默认。

## 【修订建议】
ADR-16 Consequences 补:
- 兜底机制:恢复 hooks/UserPromptSubmit,每次提交写 session_id 到 `~/.claude/.last_session_id`;cli recall/build-index 读它作 --session 默认(用户未显式覆盖时)。
- 可见性:recall 未带 session_id 时 verbose 打印警告。

Node B T_B1 拆三:T_B1_1(cli recall --session 透传)/ T_B1_2(cli 读 .last_session_id 作默认)/ T_B1_3(SKILL.md 补用法 + 兜底说明)。

verify acceptance 补:--session 显式传用显式值;未传读兜底文件;verbose 未带时警告。

**设计在此角度不成立**,必须加 hook 兜底 + 报警才能保证形式1 实际有效。
