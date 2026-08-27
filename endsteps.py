"""endsteps — CC transcript 蒸馏过滤器 (PreCompact 入库前置, 2026-08-27 重接线)。

用户裁决的新接线形态:
- CC automemory 原生机制/使用方式**完全不动** (KG 不写 CC memory 目录, 无投影)。
- PreCompact 钩子把 transcript 快照进 spool; worker 先经本模块过滤 — 只留
  **assistant 每一轮输出的 end step** — 再 autodream 入 KG。
- 召回/consolidation 全手动 (skills/memsvc)。

end step 判定 (实测校准, 717 tool_use vs 9 end_turn 的大 transcript):
- ``type == "assistant"`` 且 ``message.stop_reason == "end_turn"`` — API 语义上
  即"assistant 本轮输出自然收尾"(最终结论); tool_use 响应是中间步骤, 天然排除。
- ``isSidechain`` 默认排除 (子代理侧链, 量大且非主对话结论; env 可开)。
- content 只取 ``text`` 块 (thinking/tool_use 块不泄漏)。
- 长度门 ``MEM_ENDSTEP_MIN_CHARS`` (默认 120): 实测主链 end_turn 382–1208 字,
  寒暄应答 (「好的,继续」) 不到门槛 — 省 LLM 空转调用。
- 单文件内精确去重 (compact 恢复会重注入同文)。

输出: 每条 end step 一行 ``{"type":"user","message":{"content":...}}`` —
autodream 消费的合成 transcript 形状 (与 bootstrap.init_memory 一致)。

CLI: ``python3 endsteps.py <transcript.jsonl> [> out.jsonl]`` (无参读 stdin);
统计走 stderr。手动入库某会话结论: ``endsteps.py t.jsonl > e.jsonl && cli.py
autodream --session X --transcript e.jsonl``。
"""
from __future__ import annotations

import json
import os
import sys

DEFAULT_MIN_CHARS = 120


def extract_end_steps(lines, min_chars: int | None = None,
                      include_sidechain: bool | None = None) -> list[str]:
    """CC transcript JSONL 行迭代器 → end step 文本列表 (保序, 文内精确去重)。

    Args:
        lines: JSONL 行迭代器 (坏行静默跳过 — transcript 尾部半行常见)。
        min_chars: 长度门 (None → env MEM_ENDSTEP_MIN_CHARS → 默认 120)。
        include_sidechain: 含子代理侧链 (None → env MEM_ENDSTEP_SIDECHAIN=1)。
    """
    if min_chars is None:
        min_chars = int(os.environ.get("MEM_ENDSTEP_MIN_CHARS",
                                       str(DEFAULT_MIN_CHARS)))
    if include_sidechain is None:
        include_sidechain = os.environ.get("MEM_ENDSTEP_SIDECHAIN", "0") == "1"

    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not isinstance(d, dict) or d.get("type") != "assistant":
            continue
        if d.get("isSidechain") and not include_sidechain:
            continue
        msg = d.get("message") or {}
        if msg.get("stop_reason") != "end_turn":
            continue
        content = msg.get("content") or []
        if isinstance(content, str):  # 防御: 少见纯字符串 content
            texts = [content]
        else:
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
        txt = "\n".join(t for t in texts if t and t.strip()).strip()
        if len(txt) < min_chars:
            continue
        if txt in seen:
            continue
        seen.add(txt)
        out.append(txt)
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    src = open(argv[0], encoding="utf-8") if argv else sys.stdin
    steps = extract_end_steps(src)
    for txt in steps:
        print(json.dumps(
            {"type": "user", "message": {"content": txt}},
            ensure_ascii=False))
    print(f"endsteps: kept={len(steps)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
