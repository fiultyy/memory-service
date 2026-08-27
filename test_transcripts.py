"""M19 transcripts 适配层: cc/dsh/omp 三 harness 统一定位 + end step 蒸馏。

实测校准依据 (真实落盘):
- dsh: ~/.dsh/sessions/--home-yy-projects-memory-service--/session-<uuid>/
  session.jsonl(.zstd) 事件流 — turn/end(reason=completed) 前最后一条
  assistant/message 的 text 块; delegationDepth>0 = 子代理侧链。
- omp: ~/.omp/agent/sessions/<enc>/<ts>_<uuid>.jsonl — message.role==assistant
  ∧ stopReason=="stop" (toolUse/error/aborted 天然排除)。

共同语义: text 块 only / 长度门 120 / 文内去重 / 侧链默认排除。
"""
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import transcripts


LONG = "结论: 适配层统一三家 harness 的 end step 判定, 语义与 CC 端到端对齐。" * 5  # >120
LONG2 = "结论: 注册表按 sha 防重跑, 变更后自动重 ingest, 失败不落 sha。" * 5
SHORT = "好的。"


# ── dsh ─────────────────────────────────────────────────────────────

def _dsh_evt(t, **data):
    return json.dumps({"type": t, "seq": 0, "time": 0, "data": data})


def _dsh_assistant(text, turn=1):
    blocks = [{"type": "reasoning", "text": "隐含思考不泄漏"}]
    if text is not None:
        blocks.append({"type": "text", "text": text})
    else:
        blocks.append({"type": "tool-call", "tool": "bash"})
    return _dsh_evt("assistant/message", turn=turn,
                    message={"role": "assistant", "content": blocks})


def _mk_dsh_session(pdir, sid, events, mtime=None):
    d = pdir / f"session-{sid}"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "session.jsonl"  # 裸 jsonl = 活跃会话形态 (压缩前的落盘)
    f.write_text("\n".join(events) + "\n", encoding="utf-8")
    if mtime is not None:
        import os
        os.utime(f, (mtime, mtime))
    return f


def test_dsh_end_steps_turn_semantics():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "s.jsonl"
        evts = [
            json.dumps({"type": "session", "cwd": "/x", "delegationDepth": 0}),
            _dsh_evt("turn/start", turn=1),
            _dsh_assistant(None),                      # tool-call 步 (非终)
            _dsh_assistant("中间回答但非本回合最后一条, 应被覆盖", turn=1),
            _dsh_assistant(LONG, turn=1),              # 本回合最后一条 text
            _dsh_evt("turn/end", turn=1, reason={"kind": "completed"}),
            _dsh_evt("turn/start", turn=2),
            _dsh_assistant(LONG2, turn=2),
            _dsh_evt("turn/end", turn=2, reason={"kind": "aborted"}),  # 非完成
            _dsh_evt("turn/start", turn=3),
            _dsh_assistant(SHORT, turn=3),             # < 120 门
            _dsh_evt("turn/end", turn=3, reason={"kind": "completed"}),
            _dsh_assistant(LONG, turn=4),              # turn 外孤儿 (不计)
        ]
        p.write_text("\n".join(evts) + "\n", encoding="utf-8")
        steps = transcripts.end_steps(p, "dsh")
        assert steps == [LONG], steps  # 只留 turn1 完成 + 门/abort 过滤


def test_dsh_sidechain_gate():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "s.jsonl"
        evts = [
            json.dumps({"type": "session", "cwd": "/x", "delegationDepth": 2}),
            _dsh_assistant(LONG, turn=1),
            _dsh_evt("turn/end", turn=1, reason={"kind": "completed"}),
        ]
        p.write_text("\n".join(evts) + "\n", encoding="utf-8")
        assert transcripts.end_steps(p, "dsh") == []  # 侧链默认排除
        import os
        os.environ["MEM_ENDSTEP_SIDECHAIN"] = "1"
        try:
            assert transcripts.end_steps(p, "dsh") == [LONG]
        finally:
            del os.environ["MEM_ENDSTEP_SIDECHAIN"]


def test_dsh_locate_encoding_and_order(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cwd = "/home/yy/projects/memory-service"
    pdir = transcripts._dsh_project_dir(cwd)
    assert pdir.name == "--home-yy-projects-memory-service--"  # 实测规则
    now = time.time()
    _mk_dsh_session(pdir, "aaa", [_dsh_assistant(LONG)], mtime=now - 200)
    _mk_dsh_session(pdir, "bbb", [_dsh_assistant(LONG)], mtime=now)
    files = transcripts.locate(cwd, "dsh", limit=1)
    assert len(files) == 1 and files[0].parent.name == "session-bbb"  # mtime 降序


def test_dsh_zstd_roundtrip(tmp_path, monkeypatch):
    """zstd 形态 (压缩归档会话): zstdcat 子进程解压路径 (无 zstd 则响亮报错)。"""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cwd = "/home/yy/projects/x"
    pdir = transcripts._dsh_project_dir(cwd)
    d = pdir / "session-ccc"
    d.mkdir(parents=True)
    raw = "\n".join([
        json.dumps({"type": "session", "cwd": cwd, "delegationDepth": 0}),
        _dsh_assistant(LONG),
        _dsh_evt("turn/end", turn=1, reason={"kind": "completed"}),
    ]) + "\n"
    z = d / "session.jsonl.zstd"
    import subprocess
    if shutil.which("zstd") is None:
        raise SystemExit("zstd required for this test")
    subprocess.run(["zstd", "-q", "-o", str(z), "-"], input=raw.encode(),
                   check=True)
    assert transcripts.end_steps(z, "dsh") == [LONG]


# ── omp ─────────────────────────────────────────────────────────────

def _omp_msg(role, stop, blocks, mid="m1"):
    return json.dumps({"type": "message", "id": mid, "parentId": "p",
                       "message": {"role": role, "stopReason": stop,
                                   "content": blocks}})


def test_omp_end_steps_stop_semantics(tmp_path):
    p = tmp_path / "2026-08-27T00-00-00-000Z_00000000-0000-0000.jsonl"
    p.write_text("\n".join([
        json.dumps({"type": "session", "version": 3, "cwd": "/w"}),
        _omp_msg("user", None, [{"type": "text", "text": "任务…"}]),
        _omp_msg("assistant", "toolUse", [  # 中间步骤
            {"type": "thinking", "text": "思考不泄漏"},
            {"type": "toolCall", "tool": "bash"}]),
        _omp_msg("assistant", "stop", [  # ✓ 终答
            {"type": "thinking", "text": "思考不泄漏"},
            {"type": "text", "text": LONG}]),
        _omp_msg("assistant", "stop", [{"type": "text", "text": SHORT}]),  # 门
        _omp_msg("assistant", "stop", [{"type": "text", "text": LONG}]),  # 去重
        _omp_msg("assistant", "error", [{"type": "text", "text": LONG2}]),  # 异常
        _omp_msg("assistant", "aborted", [{"type": "text", "text": LONG2}]),
    ]) + "\n", encoding="utf-8")
    assert transcripts.end_steps(p, "omp") == [LONG]


def test_omp_locate_and_ingest_recent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cwd = str(home / "work" / "proj.alpha")  # 点保留
    pdir = transcripts._omp_project_dir(cwd)
    assert pdir.name == "-work-proj.alpha"
    f = pdir / "2026-08-27T00-00-00-000Z_00000000-0000-0000-000000000000.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text(_omp_msg("assistant", "stop",
                          [{"type": "text", "text": LONG}]) + "\n",
                 encoding="utf-8")
    assert [p.name for p in transcripts.locate(cwd, "omp")] == [f.name]
    assert transcripts.session_id(f, "omp").endswith("000000000000")

    import cli
    calls = []
    import autodream as autodream_mod
    monkeypatch.setattr(autodream_mod, "autodream",
                        lambda sid, tp, source_cwd=None: calls.append(
                            (sid, open(tp).read())) or {"added": 1})
    r = cli.ingest_recent(cwd=cwd, harness="omp",
                          registry_path=tmp_path / "reg.json")
    assert r["harness"] == "omp" and r["ingested"] == 1 and r["errors"] == 0
    assert LONG in calls[0][1]


def test_unknown_harness_loud():
    for fn in (lambda: transcripts.locate("/x", "vscode"),
               lambda: transcripts.end_steps(Path("/x"), "vscode")):
        try:
            fn()
            raise AssertionError("must raise ValueError")
        except ValueError:
            pass


def test_cc_adapter_passthrough(tmp_path):
    """cc 适配器 = endsteps 原实现单源透传 (回归锚)。"""
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "assistant", "message": {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": LONG}]}}) + "\n", encoding="utf-8")
    assert transcripts.end_steps(p, "cc") == [LONG]
    assert transcripts.session_id(p, "cc") == "t"
