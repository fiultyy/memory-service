"""endsteps — CC transcript 蒸馏过滤器 (PreCompact 入库前置, 2026-08-27 重接线)。

用户裁决的接线形态 (09-01 终裁A方案: 2026-08-27「CC automemory 不动」红线取消):
- CC automemory 投影恢复 — SessionStart synthesis-index 单点写 CC memory
  (MEMORY.md 投影索引, ADR-A 原生格式); recall --project 的 recall-<DATE>.md
  召回日志投影照旧。本模块只管 transcript 蒸馏, 不碰投影。
- PreCompact 钩子把 transcript 快照进 spool; worker 先经本模块过滤 — 只留
  **assistant 每一轮输出的 end step** — 再 autodream 入 KG。
- 召回注入走 UserPromptSubmit 钩子 (recall_inject.py), consolidation 手动
  (skills/memsvc); 手动补近期会话 = cli ingest-recent。

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
autodream --session X --transcript e.jsonl``; 批量最近 N 个会话一键:
``cli.py ingest-recent`` (本模块同口径蒸馏 + sha 注册表防重跑)。

dsh 双格式 (B1-P2, 2026-09-02): dsh 桥 PreCompact 快照为 dsh 事件流
(``user/message``/``assistant/message``/``compaction/*``), 自动识别 — 首个可
解析行 type 以 ``/message`` 结尾即按 dsh 口径蒸馏
(``transcripts._dsh_end_steps``: turn/end reason=completed 前最后 assistant
text, delegationDepth>0 侧链排除, 自带去重; 长度门同 CC)。CC 路径零改动。
"""
from __future__ import annotations

import json
import os
import sys

DEFAULT_MIN_CHARS = 120


def _looks_like_dsh(lines) -> bool:
    """前 100 个可解析行内出现 ``*/message`` 类型 → dsh 事件流 (B1-P2)。

    dsh 流首行是 ``session`` 头事件 (非消息), 故扫描一个有界窗口;
    CC 流首行即裸类型 (user/assistant/summary, 带 parentUuid) → 立即判伪。
    """
    scanned = 0
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        scanned += 1
        t = d.get("type")
        if isinstance(t, str) and t.endswith("/message"):
            return True
        if "parentUuid" in d or t in ("user", "assistant", "summary",
                                      "system", "progress"):
            return False  # CC 裸类型流
        if scanned >= 100:
            break
    return False


def distill(lines, min_chars: int | None = None) -> list[str]:
    """双格式蒸馏入口 (B1-P2): 自动识别 CC/dsh → end step 文本列表。"""
    if _looks_like_dsh(lines):
        import transcripts as transcripts_mod  # 延迟导入 (顶层互不加载)
        texts = transcripts_mod._dsh_end_steps(list(lines))
        if min_chars is None:
            min_chars = int(os.environ.get("MEM_ENDSTEP_MIN_CHARS",
                                           str(DEFAULT_MIN_CHARS)))
        return [t for t in texts if len(t) >= min_chars]
    return extract_end_steps(lines)


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


def _dsh_steps(lines, min_chars: int | None = None) -> list[str]:
    """dsh 事件流 → end step 文本 (长度门与 CC 同门槛; 去重/侧链已在
    ``transcripts._dsh_end_steps`` 内做)。"""
    import transcripts as transcripts_mod  # 延迟导入 (transcripts 顶层 import 本模块)
    texts = transcripts_mod._dsh_end_steps(lines)
    if min_chars is None:
        min_chars = int(os.environ.get("MEM_ENDSTEP_MIN_CHARS",
                                       str(DEFAULT_MIN_CHARS)))
    return [t for t in texts if len(t) >= min_chars]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # --scenes (M21, 2026-08-28, Codex 阅读优先级采纳): 输出用户声音场景 —
    # 每个 end step 配对其前累积的用户原话块, [用户]/[助手结论] 角色标记
    # (prompt v5 阅读优先级依赖)。缺省行为不变 (纯 end step, 兼容旧管道)。
    scenes_mode = "--scenes" in argv
    argv = [a for a in argv if a != "--scenes"]
    src = open(argv[0], encoding="utf-8") if argv else sys.stdin
    # B1-P2: 双格式自动识别需先判型 — 快照体量内一次性缓冲 (worker 快照
    # 有界; 手动 stdin 同样可缓冲)。
    lines = src.readlines()
    try:
        src.close()
    except Exception:
        pass
    if _looks_like_dsh(lines):
        steps = _dsh_steps(lines)
        for txt in steps:
            print(json.dumps(
                {"type": "user", "message": {"content": txt}},
                ensure_ascii=False))
        print(f"endsteps: kept={len(steps)} (dsh)", file=sys.stderr)
        return 0
    if scenes_mode:
        import transcripts as transcripts_mod  # 延迟导入 (transcripts 顶层 import 本模块)
        sc = transcripts_mod._cc_scenes(lines)
        n_blocks = 0
        for s in sc:
            for ub in s["user_blocks"]:
                print(json.dumps(
                    {"type": "user",
                     "message": {"content": f"[用户] {ub}"}},
                    ensure_ascii=False))
                n_blocks += 1
            print(json.dumps(
                {"type": "user",
                 "message": {"content": f"[助手结论] {s['end_step']}"}},
                ensure_ascii=False))
        print(f"endsteps: scenes={len(sc)} user_blocks={n_blocks}",
              file=sys.stderr)
        return 0
    steps = extract_end_steps(lines)
    for txt in steps:
        print(json.dumps(
            {"type": "user", "message": {"content": txt}},
            ensure_ascii=False))
    print(f"endsteps: kept={len(steps)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
