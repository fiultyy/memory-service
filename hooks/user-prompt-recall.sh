#!/usr/bin/env bash
# [复接线 2026-09-01] 已挂 UserPromptSubmit (settings.json 恢复接线, guarded
# pattern), 指向 09-01 终裁A方案 (08-27「hook 自动全清掉」裁决解除 — 投影恢复
# SessionStart 单点, 本钩子恢复召回注入)。
# UserPromptSubmit hook (P2): KG 召回 → additionalContext 注入。
#
# CC 每次用户提交 prompt 时触发 (stdin JSON: payload 含 prompt/session_id;
# CC ≥2.x 另含 transcript_path/cwd)。
# 全部逻辑在 hooks/recall_inject.py (stdin 透传); 本壳只做定位+守护:
#   - 脚本缺失/无 python → 静默放行 (guarded pattern, 绝不阻塞 prompt)
#   - timeout 硬顶 (env MEM_RECALL_TIMEOUT, 缺省 15s — M2/C4: 单一壳预算,
#     首轮档嵌入往返也够; 超时放弃注入, prompt 照常)
#   - **always exit 0**
# 词法召回 (search LIKE + value 扫描 + pagerank + LIF) 零 embed 依赖 —
# LM Studio 不在线也正常。延迟/阈值/预算 env 见 recall_inject.py 头注。
set -u

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INJECT="${HOOK_DIR}/recall_inject.py"

[ -f "${INJECT}" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

timeout "${MEM_RECALL_TIMEOUT:-15}" python3 "${INJECT}" 2>/dev/null
# 注入器自身保证任何失败路径都零输出 exit 0; timeout(124) 也吞掉。
exit 0
