#!/usr/bin/env bash
# spool-worker.sh — PreCompact 快照排干 worker (P1, 2026-08-27)。
#
# 单例锁 (flock spool-worker.lock) 防并发双跑; 逐个处理 spool/*.jsonl:
#   autodream(session 从文件名提取) → 成功删文件 → synthesis-index 投影
# 对账。处理中失败 (LLM 不可达等) 的文件**留在 spool** (下次钩子触发/
# 下次 compact 重试 — 不丢记忆, 只延迟); .lock 后缀文件是上次被中断
# 处理的半成品, 重命名回收重试。
#
# 排空后自行退出 — 常驻由钩子触发驱动, 无 daemon。
set -u

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SVC_DIR="$(cd "${HOOK_DIR}/.." && pwd)"
CLI="${SVC_DIR}/cli.py"
SPOOL="${SVC_DIR}/data/transcript-spool"
CWD_ARG=""
[ "${1:-}" = "--cwd" ] && [ -n "${2:-}" ] && CWD_ARG="--cwd ${2}"

[ -f "${CLI}" ] || exit 0
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

    ( cd "${SVC_DIR}" && \
      python3 cli.py autodream --session "${session}" \
                                --transcript "$f.lock" \
                                ${CWD_ARG:+--cwd "${CWD_ARG#--cwd }"} \
          >>"${SPOOL}/worker.log" 2>&1 )
    rc=$?

    if [ $rc -eq 0 ]; then
        rm -f -- "$f.lock"
        ( cd "${SVC_DIR}" && \
          python3 cli.py synthesis-index \
                ${CWD_ARG:+--scope "${CWD_ARG#--cwd }"} \
                >>"${SPOOL}/worker.log" 2>&1 || true )
    else
        # 失败: 保留重试 (去掉 .lock 回到待处理态)。
        mv -- "$f.lock" "$f" 2>/dev/null || true
        echo "$(date -Is) retry-later: ${base} rc=${rc}" >>"${SPOOL}/worker.log"
    fi
done

exit 0
