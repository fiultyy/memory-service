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

# Need cli.py and python3 to re-ingest.
if [ ! -f "${CLI}" ] || ! command -v python3 >/dev/null 2>&1; then
    exit 0
fi

# Background re-ingest with nohup (non-blocking).
# ponytail: nohup + & ensures tool completes before re-ingest finishes.
nohup python3 "${CLI}" re-ingest "${FILE_PATH}" ${CWD:+--cwd "$CWD"} >/dev/null 2>&1 &

exit 0
