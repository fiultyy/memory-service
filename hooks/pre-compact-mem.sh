#!/usr/bin/env bash
# PreCompact hook (ADR-10): transcript 快照 → 后台蒸馏入库 (v2 重接线 2026-08-27)。
#
# v2 形态 (用户裁决): CC automemory 机制/使用完全不动; 本钩子只做一件事 —
# 快照 transcript 进 spool, 由 spool-worker 蒸馏出 **assistant 每轮输出的
# end step** (stop_reason=end_turn 主链 text) 入 KG。召回/consolidation 全
# 手动 (skills/memsvc)。同步面 ~25ms (快照+nohup), compact 永不阻塞。
#
# 落库正确性: fact 级 NOOP 幂等(同 (s,p,o) 不重复 ADD) + spool 文件名
# session_id+sha16 去重 + worker 失败保留重试 (.lock 可回收)。
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
