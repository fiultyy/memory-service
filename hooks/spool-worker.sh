#!/usr/bin/env bash
# spool-worker.sh — PreCompact 快照排干 worker (P1 → 2026-08-27 v2 重接线)。
#
# v2 形态 (用户裁决: CC automemory 不动 / 只抽 assistant end step 入 KG /
# 召回+consolidation 手动):
#   1. 逐 spool 文件先经 endsteps.py 蒸馏 — 只留 assistant 每轮输出的
#      end step (stop_reason=end_turn 主链 text, 长度门 120, 文内去重),
#      合成 autodream 可吃的 transcript。
#   2. 蒸馏为空 (纯工具会话) → 视为成功, 删文件零 LLM。
#   3. autodream 入 KG (LLM 直抽, 断供响亮跳过留重试)。
#   4. **不再 synthesis-index** — 那会写 CC memory 目录 (投影/MEMORY.md),
#      违背「不改变 CC automemory 机制」; KG 是唯一 sink。
#
# 单例锁 (flock) 防并发双跑; 处理中 .lock 后缀可回收重试; 失败文件留在
# spool 下次重试 (不丢记忆, 只延迟)。排空即退出, 无 daemon。
set -u

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SVC_DIR="$(cd "${HOOK_DIR}/.." && pwd)"
CLI="${SVC_DIR}/cli.py"
ENDSTEPS="${SVC_DIR}/endsteps.py"
SPOOL="${SVC_DIR}/data/transcript-spool"
CWD_ARG=""
[ "${1:-}" = "--cwd" ] && [ -n "${2:-}" ] && CWD_ARG="--cwd ${2}"

[ -f "${CLI}" ] || exit 0
[ -f "${ENDSTEPS}" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
mkdir -p "${SPOOL}" 2>/dev/null || exit 0

# 单例锁: 非阻塞抢锁, 抢不到 = 已有 worker 在跑 → 退出。
exec 9>"${SPOOL}/worker.lock"
flock -n 9 || exit 0

# 回收上次中断的半成品 (.lock → 原名重试)。
for f in "${SPOOL}"/*.jsonl.lock; do
    [ -f "$f" ] || continue
    mv -- "$f" "${f%.lock}" 2>/dev/null || true
done

shopt -s nullglob
for f in "${SPOOL}"/*.jsonl; do
    # 文件名: <session_id>-<sha16>.jsonl — session 可能含 '-', 取到
    # 倒数第二段为止 (sha16 恒为末段)。
    base="$(basename "$f" .jsonl)"
    session="${base%-*}"
    [ -z "${session}" ] && session="unknown"

    # 占位: 处理中改名 .lock (中断可回收)。
    mv -- "$f" "$f.lock" 2>/dev/null || continue

    # ① 蒸馏: raw transcript → assistant end steps 合成 transcript。
    if ! python3 "${ENDSTEPS}" "$f.lock" > "$f.endsteps" 2>>"${SPOOL}/worker.log"; then
        mv -- "$f.lock" "$f" 2>/dev/null || true
        echo "$(date -Is) filter-fail: ${base}" >>"${SPOOL}/worker.log"
        continue
    fi

    # ② 空蒸馏 (纯工具会话/全短应答) → 成功, 零 LLM。
    if [ ! -s "$f.endsteps" ]; then
        rm -f -- "$f.lock" "$f.endsteps"
        echo "$(date -Is) no-end-steps: ${base}" >>"${SPOOL}/worker.log"
        continue
    fi

    # ③ autodream 入 KG (LLM 直抽)。
    ( cd "${SVC_DIR}" && \
      python3 cli.py autodream --session "${session}" \
                                --transcript "$f.endsteps" \
                                ${CWD_ARG:+--cwd "${CWD_ARG#--cwd }"} \
          >>"${SPOOL}/worker.log" 2>&1 )
    rc=$?

    # ④ 成功删 raw+蒸馏; 失败留 raw 重试 (蒸馏可再生, 不留)。
    if [ $rc -eq 0 ]; then
        rm -f -- "$f.lock" "$f.endsteps"
    else
        rm -f -- "$f.endsteps"
        mv -- "$f.lock" "$f" 2>/dev/null || true
        echo "$(date -Is) retry-later: ${base} rc=${rc}" >>"${SPOOL}/worker.log"
    fi
done

exit 0
