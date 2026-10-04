#!/usr/bin/env bash
# [复接线 2026-09-01] SessionStart hook: 开局对账投影 KG → CC memory
# (09-01 终裁A方案: synthesis-index 单点 — PreCompact 链只入库不投影)。
#
# (S6 解耦下沉 2026-10-04, iterate/harness-decouple): MEM_HARNESS 白名单
# 路由 + 调用平移至 runtime.project_on_start — 库面直调 (不再起 cli 子进程,
# 少一次冷启动, 落盘内容不变)。本壳只留 timeout 守护 (env
# MEM_SESSION_START_TIMEOUT 缺省 15s) + python3 存在性 + 恒 exit 0。
# 注册面 (settings.json / dsh-hooks.json) 一字未动。
set -u

SVC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if command -v python3 >/dev/null 2>&1; then
    if command -v timeout >/dev/null 2>&1; then
        cat 2>/dev/null | timeout "${MEM_SESSION_START_TIMEOUT:-15}" \
            python3 "${SVC_DIR}/runtime.py" session-start >/dev/null 2>&1 || true
    else
        cat 2>/dev/null | python3 "${SVC_DIR}/runtime.py" session-start >/dev/null 2>&1 || true
    fi
fi

exit 0
