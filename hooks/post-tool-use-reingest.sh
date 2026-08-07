#!/usr/bin/env bash
# PostToolUse hook (ADR-17 a/e/f): re-ingest CC memory md files after Write/Edit.
#
# CC fires this after Write/Edit tools (stdin JSON: {tool_name, tool_input, cwd}).
# We detect edits to CC memory dir .md files and trigger `re-ingest` to update the
# KG incrementally. Runs in background with nohup so it never blocks the tool.
# **Always exit 0** — failure is tolerated (ADR-10 Decision pattern).
#
# Tolerates: missing jq, missing python3, missing cli.py, non-memory files,
# non-Write/Edit tools. Any failure → exit 0 immediately.

set -u

# Repo root = parent of this hook's dir.
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SVC_DIR="$(cd "${HOOK_DIR}/.." && pwd)"
CLI="${SVC_DIR}/cli.py"

# Drain stdin.
STDIN="$(cat 2>/dev/null || true)"

# Parse tool_name + file_path + cwd.
TOOL_NAME=""
FILE_PATH=""
CWD=""
if command -v jq >/dev/null 2>&1; then
    TOOL_NAME="$(printf '%s' "${STDIN}" | jq -r '.tool_name // empty' 2>/dev/null || true)"
    FILE_PATH="$(printf '%s' "${STDIN}" | jq -r '.tool_input.file_path // empty' 2>/dev/null || true)"
    CWD="$(printf '%s' "${STDIN}" | jq -r '.cwd // empty' 2>/dev/null || true)"
elif command -v python3 >/dev/null 2>&1; then
    read -r TOOL_NAME FILE_PATH CWD <<EOF 2>/dev/null || true
$(printf '%s' "${STDIN}" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
    ti=d.get("tool_input",{})
except Exception:
    d={}
    ti={}
print(d.get("tool_name","") or "", ti.get("file_path","") or "", d.get("cwd","") or "")' 2>/dev/null || true)
EOF
fi

# Must be Write/Edit targeting a memory .md file.
if [ "${TOOL_NAME}" != "Write" ] && [ "${TOOL_NAME}" != "Edit" ]; then
    exit 0
fi

# CC memory dir pattern: ~/.claude/projects/*/memory/*.md
if ! printf '%s' "${FILE_PATH}" | grep -qE '^'"${HOME}"'/.claude/projects/.*/memory/.*\.md$'; then
    exit 0
fi

# #3: 从 file_path 反推 project cwd (优先于 stdin cwd,需 isdir 验证)
# file_path 格式: ~/.claude/projects/<encoded>/memory/X.md
# encoded = cwd.replace("/", "-").replace(".", "-").replace("-", "-")
# 注意: 编码不可逆! 原始的 "/" 和 "." 和连字符 "-" 都变成 "-"，无法区分。
# 反推仅对无连字符的 cwd 可靠(如 /home/yy/projects/foo),含连字符则必错(如 memory-service)。
INFERRED_CWD=""
if printf '%s' "${FILE_PATH}" | grep -qE '^'"${HOME}"'/.claude/projects/'; then
    # 抽取 encoded (projects/ 和 /memory 之间)
    ENCODED="$(printf '%s' "${FILE_PATH}" | sed -E 's|^'"${HOME}"'/.claude/projects/([^/]+)/memory/.*$|\1|')"
    if [ -n "${ENCODED}" ]; then
        # 粗略逆 encoded → cwd (实践中 cwd 多无 ".",encoded 首字符 "-" = 根 "/")
        if [ "${ENCODED}" = "-" ]; then
            INFERRED_CWD="/"
        else
            # 去掉首字符 "-" (根标记), 然后把剩余 "-" 替换回 "/"
            REST="${ENCODED#-}"  # 去首字符 "-"
            INFERRED_CWD="/$(printf '%s' "${REST}" | tr '-' '/')"
        fi
    fi
fi
# isdir 验证: 只有反推 cwd 是真实目录才采信，否则 fallback stdin cwd
# 含连字符的 cwd(如 memory-service)反推错 → isdir 失败 → 用 stdin cwd
if [ -n "${INFERRED_CWD}" ] && [ -d "${INFERRED_CWD}" ]; then
    CWD="${INFERRED_CWD}"
fi
# CWD 已设: 优先 isdir 通过的反推值，否则保留 stdin cwd

# Need cli.py and python3 to re-ingest.
if [ ! -f "${CLI}" ] || ! command -v python3 >/dev/null 2>&1; then
    exit 0
fi

# Background re-ingest with nohup (non-blocking).
# ponytail: nohup + & ensures tool completes before re-ingest finishes.
nohup python3 "${CLI}" re-ingest "${FILE_PATH}" ${CWD:+--cwd "$CWD"} >/dev/null 2>&1 &

exit 0
