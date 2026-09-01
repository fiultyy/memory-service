#!/usr/bin/env bash
# PreCompact hook (ADR-10): transcript 快照 → 后台蒸馏入库 (v2 重接线 2026-08-27)。
#
# v2 形态 (用户裁决; 2026-09-01 注: 「CC automemory 不动」红线已经 09-01 终裁
# A方案取消 — SessionStart 恢复单点投影, 与本钩子正交): 本钩子只做一件事 —
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
# MEM_SPOOL_DIR / MEM_SPOOL_WORKER: 测试注入口 (B1-P2) — 缺省行为不变。
SPOOL="${MEM_SPOOL_DIR:-${SVC_DIR}/data/transcript-spool}"

# Drain stdin (CC delivers the hook payload on fd 0).
STDIN="$(cat 2>/dev/null || true)"

TRANSCRIPT_PATH=""
SESSION_ID=""
CWD=""
COMPACT_ID=""
if command -v jq >/dev/null 2>&1; then
    TRANSCRIPT_PATH="$(printf '%s' "${STDIN}" | jq -r '.transcript_path // empty' 2>/dev/null || true)"
    SESSION_ID="$(printf '%s' "${STDIN}" | jq -r '.session_id // empty' 2>/dev/null || true)"
    CWD="$(printf '%s' "${STDIN}" | jq -r '.cwd // empty' 2>/dev/null || true)"
    COMPACT_ID="$(printf '%s' "${STDIN}" | jq -r '.compaction_id // empty' 2>/dev/null || true)"
elif command -v python3 >/dev/null 2>&1; then
    read -r TRANSCRIPT_PATH SESSION_ID CWD COMPACT_ID <<EOF 2>/dev/null || true
$(printf '%s' "${STDIN}" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    d={}
print(d.get("transcript_path","") or "", d.get("session_id","") or "",
      d.get("cwd","") or "", d.get("compaction_id","") or "")' 2>/dev/null || true)
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
# B1-P2 (2026-09-02): dsh 桥快照分支 — transcript 为 session.jsonl.zstd
# (zstd 二进制), 解压明文化落盘 (worker/*.jsonl + endsteps 双格式自动识别);
# 幂等键 = session_id + compactionId (同一 compaction 事件重放落同名文件,
# worker 原子覆盖吸收)。文件名 <sid>-<cid12>.jsonl 兼容 worker 的 session
# 解析 (取末段 '-*' 之前); 无 compaction_id → 回退内容 sha16 (CC 同款)。
if [ "$(printf '%s' "${TRANSCRIPT_PATH}" | grep -c '\.zstd$')" -eq 1 ]; then
    ZCAT="$(command -v zstdcat || command -v zstd)"
    [ -n "${ZCAT}" ] || exit 0  # 无 zstd → 放行 compact (tolerate-everything)
    CID="$(printf '%s' "${COMPACT_ID}" | tr -d '-' | cut -c1-12)"
    [ -z "${CID}" ] && CID="$(sha256sum "${TRANSCRIPT_PATH}" 2>/dev/null | cut -c1-16)"
    [ -z "${CID}" ] && CID="$$-$(date +%s)"
    SPOOL_FILE="${SPOOL}/${SESSION_ID}-${CID}.jsonl"
    if [ "$(basename "${ZCAT}")" = "zstdcat" ]; then
        "${ZCAT}" -- "${TRANSCRIPT_PATH}" 2>/dev/null > "${SPOOL_FILE}.tmp" \
            && mv -- "${SPOOL_FILE}.tmp" "${SPOOL_FILE}" 2>/dev/null || true
    else
        "${ZCAT}" -dc -- "${TRANSCRIPT_PATH}" 2>/dev/null > "${SPOOL_FILE}.tmp" \
            && mv -- "${SPOOL_FILE}.tmp" "${SPOOL_FILE}" 2>/dev/null || true
    fi
else
    SHA="$(sha256sum "${TRANSCRIPT_PATH}" 2>/dev/null | cut -c1-16)"
    [ -z "${SHA}" ] && SHA="$$-$(date +%s)"
    SPOOL_FILE="${SPOOL}/${SESSION_ID}-${SHA}.jsonl"
    cp -- "${TRANSCRIPT_PATH}" "${SPOOL_FILE}.tmp" 2>/dev/null \
        && mv -- "${SPOOL_FILE}.tmp" "${SPOOL_FILE}" 2>/dev/null || true
fi

# ② 排干积压 + 处理新快照: 单例后台 worker (锁文件防并发双跑; 排干
# spool 全部待处理文件后自行退出)。每次钩子触发都尝试拉起 — 已在跑
# 则锁失败 no-op, 幂等。MEM_SPOOL_WORKER=0 → 只快照不蒸馏 (测试/演练)。
if [ "${MEM_SPOOL_WORKER:-1}" = "1" ]; then
    nohup "${SVC_DIR}/hooks/spool-worker.sh" \
          ${CWD:+--cwd "$CWD"} >/dev/null 2>&1 &
fi

# ③ compact 永不阻塞。
exit 0
