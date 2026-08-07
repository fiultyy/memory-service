# Round 2 终稿:session_id 替代传递机制

## 【查证】
- **CC 内建 env 注入**:实测 `CLAUDE_CODE_SESSION_ID=418e378b-9abf-4fb1-a38d-ad7a97d571b4`(与 spawned session `session-418e378b` 一致)。CC 在 tool 子进程环境注入 session_id。
- PreCompact hook(pre-compact-mem.sh:34)从 stdin JSON .session_id 解析;Tool(cli.py recall)可从 `os.environ['CLAUDE_CODE_SESSION_ID']` 直接读。两者来源相同,但 **env 更可靠**(hook 依赖 stdin JSON schema 可能变)。
- cli.py:239-245 recall 子命令无 session_id;cli.py:287 调用未传。**当前拿不到是实现缺失,非 CC 限制**。
- `CLAUDE_CODE_*` 前缀是 CC 内建约定;`CLAUDE_CODE_CHILD_SESSION=1` 证明子 teammate 自动继承。

## 【结论】最可靠 + 最低侵入 = `CLAUDE_CODE_SESSION_ID` env
| 方案 | 可靠性 | 侵入性 |
|---|---|---|
| env CLAUDE_CODE_SESSION_ID | 高 | 零 |
| PreCompact stdin JSON | 中 | 低 |
| ccr/config | 低 | 高 |

## 【修订方案】(最小 diff)
1. recall() 签名加 session_id 可选参数,None 时 fallback `os.environ.get("CLAUDE_CODE_SESSION_ID", "unknown")`
2. cli.py:287 透传 session_id
3. 存储 provenance 用它(ADR-10)

**无需 hook 兜底,无需 --session 强制**。env 覆盖所有调用场景(Bash tool 子进程/teammate/普通 recall)。**推翻 round 1 grill-fragile-chain 的"必须加 hook"结论。**
