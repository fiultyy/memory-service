#!/usr/bin/env bash
# PreCompact hook (ADR-10): session transcript raw→KG incremental dream.
#
# CC fires this before a /compact (stdin JSON: {session_id, transcript_path,
# trigger, cwd}). We hand the transcript to `mem autodream` so the about-to-be-
# compressed session is consolidated into the KG as durable facts before the
# raw transcript is lost. **Always exit 0** — the hook never blocks the
# compact; autodream is best-effort side-effect (ADR-10 Decision).
#
# Tolerates everything: missing jq, missing python3, missing cli.py, a missing
# or partially-written transcript (CC writes it async — ADR-10 Consequences).
# Any failure path just returns 0 and lets the compact proceed.

set -u

# Repo root = parent of this hook's dir (hooks/ lives at <repo>/services/
# memory-service/hooks/). Used to locate cli.py in the worktree / checkout
# this hook was deployed from.
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SVC_DIR="$(cd "${HOOK_DIR}/.." && pwd)"
CLI="${SVC_DIR}/cli.py"

# Drain stdin (CC delivers the hook payload on fd 0). Keep a copy so a missing
# jq can fall back to python3 for parsing.
STDIN="$(cat 2>/dev/null || true)"

# Parse transcript_path + session_id out of the JSON payload. Prefer jq; fall
# back to python3 if jq isn't installed; if neither, give up cleanly.
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

# No transcript to dream → nothing to do, let the compact through.
[ -z "${TRANSCRIPT_PATH}" ] && exit 0

# ponytail: default session id when CC omits it (older payloads); autodream
# stamps provenance with it, an empty id just degrades the spread signal.
[ -z "${SESSION_ID}" ] && SESSION_ID="unknown"

# Run autodream only if the cli + transcript both exist. cwd = service dir
# because cli.py does same-directory bare imports (extractor/store/db — see
# SKILL.md "cli 调用约束"). Output is discarded: the hook's contract is a
# side-effect on the KG, not stdout. `set -u` already on; wrap so any error
# still falls through to exit 0.
if [ -f "${CLI}" ] && [ -r "${TRANSCRIPT_PATH}" ]; then
    if command -v python3 >/dev/null 2>&1; then
        ( cd "${SVC_DIR}" && \
          python3 cli.py autodream --session "${SESSION_ID}" \
                                    --transcript "${TRANSCRIPT_PATH}" \
                                    ${CWD:+--cwd "$CWD"} \
              >/dev/null 2>&1 || true
          # ADR-15 P2: autodream 后硬编 synthesis-index(散 index 对账 → MEMORY [mem] 唯一写入口)
          # 透传 SESSION_ID 用于轨迹 UNION(ADR-16b)
          python3 cli.py synthesis-index ${CWD:+--scope "$CWD"} --session "${SESSION_ID}" \
              >/dev/null 2>&1 || true )
    fi
fi

exit 0
