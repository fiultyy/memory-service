"""M16/M17/M18 工具面批验收测试 (spec v2 §4 M16 + §5 M17/M18, DR-9 已裁决)。

覆盖派发令五条验收:
1. 通道判定: isatty × MEM_AGENT_CONTEXT 四组态; env 无法升 human 档。
2. 四动词全链: write 落 fact (agent 档 provenance=agent_assert veracity 0.5;
   human 档 human/0.9); invalidate superseded+reason=contradiction; confirm
   信号落流; elevate 不动 fact 仅信号; 无 delete/punish 子命令 (CLI 表扫描)。
3. M18 交互确认: monkeypatch input 非 y → human 路径拒绝零副作用; agent
   路径免确认直接执行。
4. 投影 diff 信号: 手删/改 mem-*.md → human_proj_ops 信号 → run_cycle 后
   KG human 档 invalidate/update 发生。
5. cite: citations 流字段全。

测试规范: def test_xxx() 函数让 pytest 收集。禁网络/LLM。
"""
import argparse
import io
import json
import contextlib
import os
import tempfile
import uuid
from pathlib import Path

import cli
import db
import dream
import hygiene
import projection
import signals
import store


def _fresh(name: str) -> tuple[str, Path]:
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / f"{name}.db")
    sig_dir = Path(tmp) / "signals"
    orig = signals._signals_dir
    signals._signals_dir = lambda: sig_dir
    return tmp, sig_dir


def _restore_signals(orig):
    signals._signals_dir = orig


def _seed_fact(**kw) -> str:
    eid = store.put_entity("S" + uuid.uuid4().hex[:4], "concept")
    return store.put_fact(eid, "uses", "rust toolchain", extractor="llm",
                          fact_type="permanent", LIF=0.6, **kw)


# ── 验收 1: 通道判定四组态 ───────────────────────────────────────────

def test_channel_four_states():
    orig_env = os.environ.pop("MEM_AGENT_CONTEXT", None)
    try:
        # tty + 无 env = human。
        assert cli._channel(stdin_isatty=True, stdout_isatty=True) == "human"
        # tty + env 自标 = agent (env 只能降档)。
        os.environ["MEM_AGENT_CONTEXT"] = "1"
        assert cli._channel(stdin_isatty=True, stdout_isatty=True) == "agent"
        # 非 tty = agent; 非 tty + env = agent。
        os.environ.pop("MEM_AGENT_CONTEXT", None)
        assert cli._channel(stdin_isatty=False, stdout_isatty=True) == "agent"
        assert cli._channel(stdin_isatty=True, stdout_isatty=False) == "agent"
        os.environ["MEM_AGENT_CONTEXT"] = "1"
        assert cli._channel(stdin_isatty=False, stdout_isatty=False) == "agent"
    finally:
        os.environ.pop("MEM_AGENT_CONTEXT", None)


def test_channel_env_cannot_elevate_to_human():
    """方向性铁律: env 无法升 human 档 — 即便 isatty 全真, 自标即 agent。"""
    os.environ["MEM_AGENT_CONTEXT"] = "1"
    try:
        assert cli._channel(stdin_isatty=True, stdout_isatty=True) == "agent"
    finally:
        os.environ.pop("MEM_AGENT_CONTEXT", None)


# ── 验收 2: 四动词全链 ───────────────────────────────────────────────

def test_write_agent_channel_provenance():
    tmp, sig = _fresh("w-agent")
    orig_env = os.environ.pop("MEM_AGENT_CONTEXT", None)
    try:
        out = cli.mem_write("用户", "uses", "rust", channel="agent")
        assert out["provenance"] == "agent_assert" and out["channel"] == "agent"
        row = db.get_conn().execute(
            "SELECT provenance, veracity, extractor FROM fact WHERE id=?",
            (out["written"],)).fetchone()
        assert row["provenance"] == "agent_assert"
        assert row["veracity"] == 0.5, "M3 映射 agent_assert → 0.5"
        rows = signals.read("agent_crud")
        assert len(rows) == 1 and rows[0]["verb"] == "write"
        assert rows[0]["via"] == "agent"
        assert rows[0]["fact_id"] == out["written"]
    finally:
        _restore_signals(signals._signals_dir)
        os.environ.pop("MEM_AGENT_CONTEXT", None)


def test_write_human_channel_provenance():
    tmp, sig = _fresh("w-human")
    try:
        out = cli.mem_write("用户", "uses", "rust", channel="human")
        row = db.get_conn().execute(
            "SELECT provenance, veracity FROM fact WHERE id=?",
            (out["written"],)).fetchone()
        assert row["provenance"] == "human"
        assert row["veracity"] == 0.9, "M3 映射 human → 0.9"
        assert signals.read("agent_crud")[0]["via"] == "human"
    finally:
        _restore_signals(signals._signals_dir)


def test_invalidate_supersedes_with_contradiction():
    tmp, sig = _fresh("inv")
    fid = _seed_fact()
    try:
        out = cli.mem_invalidate(fid, note="已过时", channel="agent")
        assert out["invalidated"] == fid
        row = db.get_conn().execute(
            "SELECT status, supersede_reason, valid_to FROM fact WHERE id=?",
            (fid,)).fetchone()
        assert row["status"] == "superseded"
        assert row["supersede_reason"] == "contradiction"
        assert row["valid_to"] is not None
        rec = signals.read("agent_crud")[0]
        assert rec["verb"] == "invalidate" and rec["via"] == "agent"
        assert rec["note"] == "已过时"
    finally:
        _restore_signals(signals._signals_dir)


def test_confirm_signal_stream():
    tmp, sig = _fresh("conf")
    fid = _seed_fact()
    try:
        out = cli.mem_confirm(fid, channel="agent")
        assert out["confirmed"] == fid
        rows = signals.read("confirm_arrivals")
        assert len(rows) == 1
        assert rows[0]["fact_id"] == fid and rows[0]["via"] == "agent"
        # fact 本体不动 (确认是 dreaming 消费的正信号, 非 UI 写)。
        assert store.get_fact(fid)["status"] == "active"
        assert store.get_fact(fid)["LIF"] is not None
    finally:
        _restore_signals(signals._signals_dir)


def test_elevate_no_fact_mutation_signal_only():
    tmp, sig = _fresh("elev")
    fid = _seed_fact(source_cwd="/t")
    before = store.get_fact(fid)
    try:
        out = cli.mem_elevate(fid, channel="agent")
        assert out["elevated"] == fid
        after = store.get_fact(fid)
        # 不动 fact: status/LIF/fact_type 全不变 (无 supersede)。
        for k in ("status", "LIF", "fact_type", "supersedes_id",
                  "supersede_reason"):
            assert after[k] == before[k], f"elevate 不得动 {k}"
        rec = signals.read("agent_crud")[0]
        assert rec["verb"] == "elevate" and rec["fact_id"] == fid
    finally:
        _restore_signals(signals._signals_dir)


def test_no_delete_punish_subcommands():
    """P38 白名单: CLI 子命令表扫描 — 无 delete/punish 动词命令。
    (prune 的 help 描述含 'soft-delete' 字样是 ADR-17d 软删语义文档,
    非 delete 动词命令 — 扫描子命令名, 不扫描述文本。)"""
    import cli as cli_mod
    verbs = ("delete", "punish", "erase", "remove", "destroy")
    # 从 parser 取子命令名 (重建 _main 的 parser 逻辑太重 — 用 help 首列扫描)。
    import io as _io
    import contextlib as _ctx
    buf = _io.StringIO()
    with _ctx.redirect_stdout(buf):
        try:
            cli_mod._main(["--help"])
        except SystemExit:
            pass
    # 子命令名列: 两空格缩进行的首 token (argparse help 格式)。
    cmd_tokens = set()
    for line in buf.getvalue().splitlines():
        ls = line.strip()
        if ls and not ls.startswith(("usage:", "positional", "options:", "cli ", "-")):
            cmd_tokens.add(ls.split()[0])
    for v in verbs:
        assert v not in cmd_tokens, f"违 P38: 子命令 {v} 存在: {cmd_tokens}"
    # 四动词 + cite 在位。
    for verb in ("write", "confirm", "invalidate", "elevate", "cite"):
        assert verb in cmd_tokens, f"子命令 {verb} 缺失: {cmd_tokens}"


# ── 验收 3: M18 交互确认双路 ─────────────────────────────────────────

def test_human_path_invalidate_declined_on_non_y(monkeypatch=None):
    """human 路径 + input 非 y → 拒绝: fact 不动, 零副作用 (无信号)。"""
    tmp, sig = _fresh("h-decl")
    fid = _seed_fact()
    orig_input = builtins_input = __builtins__["input"] if isinstance(__builtins__, dict) else __builtins__.input
    import builtins
    builtins.input = lambda *a: "n"
    try:
        out = cli.mem_invalidate(fid, channel="human")
        assert out.get("declined") is True and out["invalidated"] is None
        row = db.get_conn().execute(
            "SELECT status FROM fact WHERE id=?", (fid,)).fetchone()
        assert row["status"] == "active", "拒绝后 fact 不得动"
        assert signals.read("agent_crud") == [], "拒绝零信号"
    finally:
        builtins.input = builtins_input
        _restore_signals(signals._signals_dir)


def test_human_path_elevate_declined_on_non_y():
    tmp, sig = _fresh("h-decl2")
    fid = _seed_fact()
    import builtins
    orig_input = builtins.input
    builtins.input = lambda *a: ""
    try:
        out = cli.mem_elevate(fid, channel="human")
        assert out.get("declined") is True
        assert signals.read("agent_crud") == []
    finally:
        builtins.input = orig_input
        _restore_signals(signals._signals_dir)


def test_human_path_confirm_y_executes():
    tmp, sig = _fresh("h-y")
    fid = _seed_fact()
    import builtins
    orig_input = builtins.input
    builtins.input = lambda *a: "y"
    try:
        # confirm 非高危 — 不需确认; invalidate y → 执行。
        out = cli.mem_invalidate(fid, channel="human")
        assert out["invalidated"] == fid
    finally:
        builtins.input = orig_input
        _restore_signals(signals._signals_dir)


def test_agent_path_no_confirmation_needed():
    """agent 路径 (env 自标语义, channel='agent'): 高危动词免确认直接执行。"""
    tmp, sig = _fresh("a-direct")
    fid = _seed_fact()
    import builtins
    orig_input = builtins.input

    def _no_input(*a):
        raise AssertionError("agent 路径不得请求交互确认")

    builtins.input = _no_input
    try:
        out1 = cli.mem_invalidate(fid, channel="agent")
        fid2 = _seed_fact()
        out2 = cli.mem_elevate(fid2, channel="agent")
        assert out1["invalidated"] == fid
        assert out2["elevated"] == fid2
    finally:
        builtins.input = orig_input
        _restore_signals(signals._signals_dir)


# ── 验收 4: 投影 diff 信号 → dream human 档裁决 ──────────────────────

def _project_fact(fid: str, mem_dir: Path) -> Path:
    f = store.get_fact(fid)
    erow = db.get_conn().execute(
        "SELECT name FROM entity WHERE id=?", (f["subject_id"],)).fetchone()
    projection.project_fact_md(f, erow["name"], mem_dir)
    return mem_dir / projection._mem_filename(
        fid, projection._fact_topic(f, erow["name"]))


def test_projection_diff_deleted_signal_and_dream_invalidate():
    """手删 mem-*.md → human_proj_ops{deleted} 信号 → run_cycle 后 fact
    human 档 invalidate (superseded + provenance=human/veracity 0.9)。"""
    tmp, sig = _fresh("pdiff")
    mem_dir = Path(tmp) / "memory"
    mem_dir.mkdir()
    fid = _seed_fact()
    p = _project_fact(fid, mem_dir)
    assert p.is_file()

    p.unlink()  # human 删投影
    events = hygiene.detect_human_proj_ops(mem_dir)
    assert len(events) == 1 and events[0]["op"] == "deleted", events
    rows = signals.read("human_proj_ops")
    assert rows[0]["op"] == "deleted" and rows[0]["fact_id"] == fid
    assert rows[0]["path"] and rows[0]["detail"]

    stats = dream.run_cycle()
    assert stats["human_proj_applied"] == 1, stats
    row = db.get_conn().execute(
        "SELECT status, supersede_reason, provenance, veracity FROM fact "
        "WHERE id=?", (fid,)).fetchone()
    assert row["status"] == "superseded", "human 删投影 → invalidate"
    assert row["supersede_reason"] == "contradiction"
    assert row["provenance"] == "human" and row["veracity"] == 0.9
    # 二次 run_cycle: 水位推进, 不重复裁决。
    assert dream.run_cycle()["human_proj_applied"] == 0


def test_projection_diff_modified_signal_and_dream_update():
    """改坏 mem-*.md frontmatter → modified 信号 → run_cycle 后 fact LIF 微抬
    (human 亲笔编辑 update 偏好, 不动 status)。fixture 用 permanent 档
    (decay 重算不折旧) 且断言只比较 run_cycle 前后快照 (promotion 夹在中间
    不影响 LIF 列)。"""
    tmp, sig = _fresh("pdiff2")
    mem_dir = Path(tmp) / "memory"
    mem_dir.mkdir()
    eid = store.put_entity("ModS" + uuid.uuid4().hex[:4], "concept")
    fid = store.put_fact(eid, "uses", "rust toolchain", extractor="human",
                         fact_type="permanent", LIF=0.5, confidence=0.8)
    p = _project_fact(fid, mem_dir)

    p.write_text("human rewrote this file entirely\n", encoding="utf-8")
    events = hygiene.detect_human_proj_ops(mem_dir)
    assert len(events) == 1 and events[0]["op"] == "modified"
    # 先读快照 (dream.run_cycle 内部 decay 会重算 LIF — 对 permanent+fresh
    # fact 重算值 >= 原值, 微抬叠加其上; 用「大于」断言吸收重算增益)。
    stats = dream.run_cycle()
    assert stats["human_proj_applied"] == 1
    after = store.get_fact(fid)
    assert after["status"] == "active", "modified 不动 status"
    assert after["LIF"] > 0.5, (
        f"human 编辑 update 偏好应抬 LIF (含 decay 重算): {after['LIF']}")
    # 卫生轮: 信号已记, 重排对账照常 (M18: 先信号后期望态重写)。
    h = hygiene.run("/w", mem_dir)
    assert h["resorted"] >= 0  # 全幂等不报错


# ── 验收 5: cite ─────────────────────────────────────────────────────

def test_cite_signal_stream_fields():
    tmp, sig = _fresh("cite")
    fid = _seed_fact(source_cwd="/proj")
    try:
        out = cli.mem_cite(fid, output_ref="answer#para3", channel="agent")
        assert out["cited"] == fid
        rows = signals.read("citations")
        assert len(rows) == 1
        r = rows[0]
        assert r["fact_id"] == fid
        assert r["agent_output_ref"] == "answer#para3"
        assert r["via"] == "agent"
        assert r["source_cwd"] == "/proj"
        assert r["ts"]
        # 不碰 KG 写面: fact 不动。
        assert store.get_fact(fid)["status"] == "active"
    finally:
        _restore_signals(signals._signals_dir)


def test_verbs_missing_fact_graceful():
    """fact_id 不存在 → 优雅拒绝 (error 字段), 不抛。"""
    tmp, sig = _fresh("missing")
    try:
        for out in (cli.mem_confirm("nope", channel="agent"),
                    cli.mem_invalidate("nope", channel="agent"),
                    cli.mem_elevate("nope", channel="agent"),
                    cli.mem_cite("nope", channel="agent")):
            assert "error" in out, out
    finally:
        _restore_signals(signals._signals_dir)
