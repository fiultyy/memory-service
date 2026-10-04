#!/usr/bin/env bash
# PreCompact hook (ADR-10): transcript 快照 → spool (v2 重接线 2026-08-27)。
#
# 职责: 快照 transcript 进 spool 池, 由 mem_daemon 段级消费蒸馏入 KG
# (H1 切换 2026-10-01: 本钩子纯快照; 旧 spool-worker 通道已下线)。
# 落库正确性: spool 文件名 session_id+sha16|cid12 幂等去重 + 原子 tmp+mv。
#
# (S5 解耦下沉 2026-10-04, iterate/harness-decouple): 核心块 (payload 解析
# / dsh 回查 / zstd 明文化 / 幂等命名 / sidecar) 平移至 runtime.py
# snapshot_transcript — 语义钉子由 tests/test_golden_hooks.py 黄金重放
# 逐字节断言 (sha16 ≡ sha256sum 前16, cid12 = compaction_id 去'-'前12,
# ls -1t ≡ mtime 降序取首)。本壳只留守护: python3 存在性 + 恒 exit 0
# (tolerate-everything; compact 永不阻塞)。已知等价性边界: 无 python3 有
# jq 的环境失去快照 — cc/dsh 注册面本就跑 python, 接受。
# 注册面 (settings.json / dsh-hooks.json) 一字未动。
set -u

SVC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Drain stdin (CC delivers the hook payload on fd 0) → python 单点消费。
if command -v python3 >/dev/null 2>&1; then
    cat 2>/dev/null | python3 "${SVC_DIR}/runtime.py" precompact 2>/dev/null || true
fi

# compact 永不阻塞。
exit 0
