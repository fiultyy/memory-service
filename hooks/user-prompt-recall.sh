#!/usr/bin/env bash
# [休眠 2026-08-27] CC 接线已全部移除 (用户裁决「hook 自动全清掉」) — 本脚本不再被
# 任何 settings.json 触发, 仅作手动/未来重接线工具保留 (同 regex 通道模式)。
# UserPromptSubmit hook (P2): KG 召回 → additionalContext 注入。
#
# CC 每次用户提交 prompt 时触发 (stdin JSON: {prompt, session_id, cwd})。
# 全部逻辑在 hooks/recall_inject.py (stdin 透传); 本壳只做定位+守护:
#   - 脚本缺失/无 python → 静默放行 (guarded pattern, 绝不阻塞 prompt)
#   - 2s 硬顶: 超时放弃注入, prompt 照常 (增强面不挡主路)
#   - **always exit 0**
# 词法召回 (search LIKE + value 扫描 + pagerank + LIF) 零 embed 依赖 —
# LM Studio 不在线也正常。延迟/阈值/预算 env 见 recall_inject.py 头注。
set -u

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INJECT="${HOOK_DIR}/recall_inject.py"

[ -f "${INJECT}" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

timeout 2 python3 "${INJECT}" 2>/dev/null
# 注入器自身保证任何失败路径都零输出 exit 0; timeout(124) 也吞掉。
exit 0
