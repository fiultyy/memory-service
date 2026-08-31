"""endsteps 过滤器测试 — end step 判定/长度门/侧链/去重/防御路径。

新接线形态 (09-01 终裁A方案): PreCompact 只抽 assistant 每轮输出的 end step
入 KG; CC automemory 投影恢复 (SessionStart 单点); 召回注入走 UserPromptSubmit,
consolidation 手动。"""
import json

import endsteps


def _assistant(text=None, stop="end_turn", sidechain=False, tool=False,
               thinking=None):
    blocks = []
    if thinking is not None:
        blocks.append({"type": "thinking", "thinking": thinking})
    if tool:
        blocks.append({"type": "tool_use", "id": "t1", "name": "Bash",
                       "input": {"command": "ls"}})
    if text is not None:
        blocks.append({"type": "text", "text": text})
    msg = {"role": "assistant", "content": blocks, "stop_reason": stop}
    d = {"type": "assistant", "message": msg}
    if sidechain:
        d["isSidechain"] = True
    return json.dumps(d, ensure_ascii=False)


def _user(text):
    return json.dumps({"type": "user",
                       "message": {"role": "user", "content": text}})


LONG = "结论: memsvc 的向量召回走 sqlite-vec, 余弦度量, 维度 2560, " \
       "与 LM Studio 的 qwen3-embedding-4b 配合, 词法层零依赖离线可用; " \
       "词法路径完全离线, 不依赖任何远端服务, 长程检索零成本可重放。"


def test_end_turn_text_kept():
    lines = [_user("问"), _assistant(LONG), _user("next")]
    assert endsteps.extract_end_steps(lines, min_chars=10) == [LONG]


def test_tool_use_steps_dropped():
    """中间步骤 (stop_reason=tool_use) 不入库 — 即使带 text 前导。"""
    lines = [_assistant("我先看看文件", stop="tool_use", tool=True),
             _assistant(LONG)]
    assert endsteps.extract_end_steps(lines, min_chars=10) == [LONG]


def test_thinking_block_not_leaked():
    lines = [_assistant(LONG, thinking="内部推理过程不应入库")]
    got = endsteps.extract_end_steps(lines, min_chars=10)
    assert got == [LONG] and "内部推理" not in got[0]


def test_sidechain_dropped_by_default():
    lines = [_assistant(LONG, sidechain=True), _assistant(LONG + " 主链")]
    got = endsteps.extract_end_steps(lines, min_chars=10)
    assert got == [LONG + " 主链"]


def test_sidechain_included_via_flag():
    lines = [_assistant(LONG, sidechain=True)]
    assert endsteps.extract_end_steps(
        lines, min_chars=10, include_sidechain=True) == [LONG]


def test_min_chars_gate():
    lines = [_assistant("好的，继续"), _assistant(LONG)]
    got = endsteps.extract_end_steps(lines, min_chars=120)
    assert got == [LONG]  # 寒暄被长度门拦


def test_exact_dedup_within_file():
    lines = [_assistant(LONG), _assistant(LONG)]
    assert endsteps.extract_end_steps(lines, min_chars=10) == [LONG]


def test_multi_text_blocks_joined():
    two = "第一段结论。" + "x" * 200 + "\n 第二段补充。"
    lines = [_assistant(None)]
    # 手工构造双 text 块
    d = {"type": "assistant", "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "第一段结论。" + "x" * 200},
                    {"type": "text", "text": " 第二段补充。"}],
        "stop_reason": "end_turn"}}
    lines = [json.dumps(d, ensure_ascii=False)]
    assert endsteps.extract_end_steps(lines, min_chars=10) == [two]


def test_bad_lines_and_non_assistant_skipped():
    lines = ["{broken json", _user("hi"),
             json.dumps({"type": "system", "content": "sys"}),
             _assistant(LONG)]
    assert endsteps.extract_end_steps(lines, min_chars=10) == [LONG]


def test_string_content_defensive():
    d = json.dumps({"type": "assistant", "message": {
        "role": "assistant", "content": LONG, "stop_reason": "end_turn"}})
    assert endsteps.extract_end_steps([d], min_chars=10) == [LONG]


def test_output_shape_autodream_compatible(tmp_path, capsys):
    """CLI 输出行 = bootstrap 合成 transcript 形状 (autodream 可直接吃)。"""
    f = tmp_path / "t.jsonl"
    f.write_text(_assistant(LONG) + "\n" + _user("x") + "\n", encoding="utf-8")
    rc = endsteps.main([str(f)])
    out = capsys.readouterr()
    assert rc == 0
    lines = [json.loads(l) for l in out.out.strip().splitlines()]
    assert lines == [{"type": "user", "message": {"content": LONG}}]
