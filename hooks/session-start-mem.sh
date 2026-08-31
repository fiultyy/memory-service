#!/usr/bin/env bash
# [复接线 2026-09-01] 已挂 SessionStart (settings.json 恢复接线, guarded
# pattern), 指向 09-01 终裁A方案 (红线取消, 恢复 synthesis-index 单点自动投影;
# 不做 T1 lazy reconcile)。
# SessionStart hook: 开局投影 KG → CC memory.
#
# CC fires this at session start (stdin JSON: {session_id, cwd}). We project
# existing KG facts into CC memory via `synthesis-index` (09-01 终裁A方案:
# SessionStart 是 MEMORY.md 投影单点 — PreCompact 链只入库不投影, 避免双写).
# **Always exit 0** — the hook never
# blocks session start; synthesis-index is best-effort side-effect.
#
# Tolerates everything: missing jq, missing python3, missing cli.py, missing
# timeout(1). Any failure path just returns 0 and lets the session proceed.

set -u

# Repo root = parent of this hook's dir (hooks/ lives at <repo>/hooks/).
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SVC_DIR="$(cd "${HOOK_DIR}/.." && pwd)"
CLI="${SVC_DIR}/cli.py"

# Drain stdin (CC delivers the hook payload on fd 0).
STDIN="$(cat 2>/dev/null || true)"

# Parse session_id + cwd from JSON payload. Prefer jq; fallback to python3.
SESSION_ID=""
CWD=""
if command -v jq >/dev/null 2>&1; then
    SESSION_ID="$(printf '%s' "${STDIN}" | jq -r '.session_id // empty' 2>/dev/null || true)"
    CWD="$(printf '%s' "${STDIN}" | jq -r '.cwd // empty' 2>/dev/null || true)"
elif command -v python3 >/dev/null 2>&1; then
    read -r SESSION_ID CWD <<EOF 2>/dev/null || true
$(printf '%s' "${STDIN}" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    d={}
print(d.get("session_id","") or "", d.get("cwd","") or "")' 2>/dev/null || true)
EOF
fi

# ponytail: default session id when CC omits it.
[ -z "${SESSION_ID}" ] && SESSION_ID="unknown"

# Run synthesis-index only if cli.py exists. Project KG → CC memory.
# Output discarded; hook's contract is side-effect on CC memory, not stdout.
# timeout 守护 (env MEM_SESSION_START_TIMEOUT, 缺省 15s): 投影超时即放弃,
# session start 不被拖住; 无 timeout(1) 的环境降级为无守护照跑 (仍 exit 0)。
if [ -f "${CLI}" ]; then
    if command -v python3 >/dev/null 2>&1; then
        if command -v timeout >/dev/null 2>&1; then
            ( cd "${SVC_DIR}" && \
              timeout "${MEM_SESSION_START_TIMEOUT:-15}" \
                  python3 cli.py synthesis-index ${CWD:+--scope "$CWD"} --session "${SESSION_ID}" \
                  >/dev/null 2>&1 || true )
        else
            ( cd "${SVC_DIR}" && \
              python3 cli.py synthesis-index ${CWD:+--scope "$CWD"} --session "${SESSION_ID}" \
                  >/dev/null 2>&1 || true )
        fi
    fi
fi

exit 0
