#!/usr/bin/env bash
# PreCompact hook (ADR-10): session transcript raw→KG incremental dream.
#
# P1 快照-后台模式 (2026-08-27, 用户裁决 P1-4 go): LLM 直抽主径下一段
# 12-60s, 长会话几十段 = 分钟级~小时级 — 同步跑必撞 CC hook 超时(默认
# 60s), 超时钩子被杀 + compact 照常执行 + 原始 transcript 被压缩掉 =
# 记忆丢失竞态 (regex 时代本钩子是快的, LLM 主径后成雷)。
#
# 本钩子只做: ①毫秒级快照 transcript → spool/ ②顺带排干已有积压
# (启动/重启后台 worker, 幂等) ③立即 exit 0。compact 永不被阻塞。
#
# 落库正确性由既有机制保证: fact 级 NOOP 幂等(同 (s,p,o) 不重复 ADD),
# 重放安全; spool 文件名含 session_id+sha256(前16), 同 transcript 重复
# 快照天然去重; worker 处理成功后删 spool 文件 (处理中加 .lock 后缀)。
#
# Tolerates everything: missing jq/python3/cli.py/transcript — exit 0。
set -u

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SVC_DIR="$(cd "${HOOK_DIR}/.." && pwd)"
CLI="${SVC_DIR}/cli.py"
SPOOL="${SVC_DIR}/data/transcript-spool"

# Drain stdin (CC delivers the hook payload on fd 0).
STDIN="$(cat 2>/dev/null || true)"

TRANSCRIPT_PATH=""
SESSION_ID=""
CWD=""
if command -v jq >/dev/null 2>&1; then
    TRANSCRIPT_PATH="$(printf '%s' "${STDIN}" | jq -r '.transcript_path // empty' 2>/dev/null || true)"
    SESSION_ID="$(printf '%s' "${STDIN}" | jq -r '.session_id // empty' 2>/dev/null || true)"
    CWD="$(printf '%s' "${STDIN}" | jq -r '.cwd // empty' 2>/dev/null || true)"
elif command -v python3 >/dev/null 2>&1; then
    read -r TRANSCRIPT_PATH SESSION_ID CWD <<EOF 2>/dev/null || true
$(printf '%s' "${STDIN}" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    d={}
print(d.get("transcript_path","") or "", d.get("session_id","") or "", d.get("cwd","") or "")' 2>/dev/null || true)
EOF
fi

# No transcript / no cli → nothing to do, let the compact through.
[ -z "${TRANSCRIPT_PATH}" ] && exit 0
[ -f "${CLI}" ] || exit 0
[ -r "${TRANSCRIPT_PATH}" ] || exit 0
[ -z "${SESSION_ID}" ] && SESSION_ID="unknown"

# ① 快照 (毫秒级): spool/<session>-<sha16>.jsonl — 同 transcript 重复
# 快照落同名文件 (原子覆盖), 天然去重不重复花钱。
mkdir -p "${SPOOL}" 2>/dev/null || true
SHA="$(sha256sum "${TRANSCRIPT_PATH}" 2>/dev/null | cut -c1-16)"
[ -z "${SHA}" ] && SHA="$$-$(date +%s)"
SPOOL_FILE="${SPOOL}/${SESSION_ID}-${SHA}.jsonl"
cp -- "${TRANSCRIPT_PATH}" "${SPOOL_FILE}.tmp" 2>/dev/null \
    && mv -- "${SPOOL_FILE}.tmp" "${SPOOL_FILE}" 2>/dev/null || true

# ② 排干积压 + 处理新快照: 单例后台 worker (锁文件防并发双跑; 排干
# spool 全部待处理文件后自行退出)。每次钩子触发都尝试拉起 — 已在跑
# 则锁失败 no-op, 幂等。
nohup "${SVC_DIR}/hooks/spool-worker.sh" \
      ${CWD:+--cwd "$CWD"} >/dev/null 2>&1 &

# ③ compact 永不阻塞。
exit 0
