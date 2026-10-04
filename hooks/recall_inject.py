"""UserPromptSubmit 注入 shim (S4 解耦下沉 2026-10-04, 分支 iterate/harness-decouple)。

业务体 (档位判定/实体锚定门/确定性 lane/配额/LIF 记账/预算裁剪) 已平移至
``runtime.inject_context`` (L2 SDK, harness 盲); 完整设计裁决头注 (时序终裁/
turn 判据/锚定门红线/分账语义/env 面) 见 git 历史 (本文件 514 行版本) 与
runtime.py 各函数 docstring。

本文件保留两个职责 (注册面 hooks/user-prompt-recall.sh 一字不动):
1. python 直跑入口 — stdin CC payload → stdout additionalContext;
2. 符号 re-export 兼容 (tests 既有引用: _det_lanes 等)。
"""
from __future__ import annotations

import sys
from pathlib import Path

SVC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SVC_DIR))

# 符号兼容 re-export (实现单源在 runtime)
from runtime import (  # noqa: F401,E402
    _count_user_turns,
    _count_user_turns_dsh,
    _det_lanes,
    _log_fail,
    _probe_rw,
    emit_context,
    hook_main,
    inject_context,
    read_payload,
)


def main() -> int:
    return hook_main(["user-prompt"])


if __name__ == "__main__":
    sys.exit(main())
