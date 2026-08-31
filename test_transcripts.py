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
                        lambda sid, tp, source_cwd=None, harness="cc":
                        calls.append((sid, open(tp).read())) or {"added": 1})
    r = cli.ingest_recent(cwd=cwd, harness="omp",
                          registry_path=tmp_path / "reg.json")
    assert r["harness"] == "omp" and r["ingested"] == 1 and r["errors"] == 0
    assert LONG in calls[0][1]
    # M21 用户声音通道: 合成 transcript 带角色标记 (prompt v5 阅读优先级依赖)
    assert "[助手结论] " + LONG in calls[0][1]


def test_pi_adapter_dsh_style_encoding(tmp_path, monkeypatch):
    """pi: 目录编码与 dsh 同规则 (-X--, 点保留); 判定与 omp 同 (stop)。"""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cwd = "/home/yy/.omp"  # 真实存在的 pi 项目 (实测目录 --home-yy-.omp--)
    pdir = transcripts._pi_project_dir(cwd)
    assert pdir.name == "--home-yy-.omp--"
    assert str(pdir).startswith(str(tmp_path / "home" / ".pi"))
    f = pdir / "2026-08-11T02-17-56-877Z_019fee9c-0000-0000.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text("\n".join([
        _omp_msg("assistant", "toolUse", [{"type": "toolCall", "tool": "b"}]),
        _omp_msg("assistant", "stop", [{"type": "text", "text": LONG}]),
        _omp_msg("assistant", "stop", [{"type": "text", "text": SHORT}]),  # 门
    ]) + "\n", encoding="utf-8")
    assert transcripts.end_steps(f, "pi") == [LONG]  # 与 omp 判定共用
    assert transcripts.session_id(f, "pi") == f.stem
    assert [p for p in transcripts.locate(cwd, "pi")] == [f]


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


# ── M21 用户声音场景 (2026-08-28, Codex 阅读优先级采纳) ───────────────

def _cc_rec(t, content, sidechain=False, stop_reason=None):
    return json.dumps({"type": t, "isSidechain": sidechain,
                       "message": {"role": t, "stop_reason": stop_reason,
                                   "content": content}}, ensure_ascii=False)


def test_cc_scenes_pairing_and_recent_cap():
    """每个 end step 配对其前累积用户块; cap 保最近 4 块 (时间序)。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "s.jsonl"
        u1, u2, u3, u4, u5 = ("用户语境一", "用户语境二", "用户语境三",
                              "用户语境四", "用户语境五")
        p.write_text("\n".join([
            _cc_rec("user", u1),
            _cc_rec("assistant", [{"type": "text", "text": LONG}],
                    stop_reason="end_turn"),
            _cc_rec("user", u2), _cc_rec("user", u3),
            _cc_rec("user", u4), _cc_rec("user", u5),
            _cc_rec("assistant", [{"type": "text", "text": LONG2}],
                    stop_reason="end_turn"),
        ]) + "\n", encoding="utf-8")
        sc = transcripts.scenes(p, "cc")
        assert [s["end_step"] for s in sc] == [LONG, LONG2]
        assert sc[0]["user_blocks"] == [u1]
        assert sc[1]["user_blocks"] == [u2, u3, u4, u5]  # 保最近, 时间序


def test_cc_scenes_cleans_injected_blocks_and_tool_results():
    """user 块过 corpus_prep (system-reminder 剥); tool_result 块不混入
    (cc 的 tool_result 由 user role 携带, _texts_of 只取 text 块)。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "s.jsonl"
        p.write_text("\n".join([
            _cc_rec("user",
                    "<system-reminder>注入上下文</system-reminder>真裁决: 先杀掉"),
            _cc_rec("user", [{"type": "tool_result",
                              "content": "工具观测不应进用户语料"}]),
            _cc_rec("assistant", [{"type": "text", "text": LONG}],
                    stop_reason="end_turn"),
        ]) + "\n", encoding="utf-8")
        sc = transcripts.scenes(p, "cc")
        assert len(sc) == 1
        assert sc[0]["user_blocks"] == ["真裁决: 先杀掉"]
        assert "工具观测" not in "".join(sc[0]["user_blocks"])


def test_dsh_scenes_structural_source_kind_filter():
    """dsh 结构化注入判别 (288 文件扫描: source.kind 是最强判别器) —
    只有 kind=user 是真人; skill-catalog/plugin(compact) 注入全弃。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "s.jsonl"

        def _user(txt, kind):
            return _dsh_evt("user/message", turn=1,
                            source={"kind": kind},
                            message={"role": "user",
                                     "content": [{"type": "text", "text": txt}]})

        def _user_real_shape(txt, kind):
            """实测形态 (session-4a37e718): content 直挂 data, message 键缺省。"""
            return _dsh_evt("user/message", turn=1,
                            source={"kind": kind, "rpcId": "r1"},
                            content=[{"type": "text", "text": txt}],
                            role="user")

        p.write_text("\n".join([
            json.dumps({"type": "session", "cwd": "/x", "delegationDepth": 0}),
            _user("用户真实语境: 端口挪到 8766", "user"),
            _user_real_shape("实测形态: 先杀掉常驻, 手动拉起", "user"),
            _user("<system-reminder>skill 目录注入</system-reminder>", "skill-catalog"),
            _user("<compacted-summary>压缩重注入</compacted-summary>", "plugin"),
            _dsh_assistant(LONG, turn=1),
            _dsh_evt("turn/end", turn=1, reason={"kind": "completed"}),
        ]) + "\n", encoding="utf-8")
        sc = transcripts.scenes(p, "dsh")
        assert len(sc) == 1
        assert sc[0]["user_blocks"] == ["用户真实语境: 端口挪到 8766",
                                        "实测形态: 先杀掉常驻, 手动拉起"]
        assert sc[0]["end_step"] == LONG


def test_pi_scenes_envelope_unwrap():
    """pi 桥信封 (62% user 文本的污染源): bridge_* 剥, user_input 拆包保内文。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "s.jsonl"
        env = ('<bridge_context>\n{"chatId":"oc_x","senderId":"ou_y"}\n'
               '</bridge_context>\n<user_input>\n{"text":"桥上真实输入语境"}\n</user_input>')
        p.write_text("\n".join([
            _omp_msg("user", None, [{"type": "text", "text": env}]),
            _omp_msg("assistant", "stop", [{"type": "text", "text": LONG}]),
        ]) + "\n", encoding="utf-8")
        sc = transcripts.scenes(p, "pi")
        assert len(sc) == 1 and sc[0]["end_step"] == LONG
        assert sc[0]["user_blocks"] == ['{"text":"桥上真实输入语境"}']


def test_scenes_unknown_harness_loud():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        try:
            transcripts.scenes(Path(td) / "x.jsonl", "vscode")
            raise AssertionError("must raise ValueError")
        except ValueError:
            pass


# ── M22 codex 适配器 (yaml 节点3 过滤逻辑映射) ────────────────────────

def _codex_line(t, **payload):
    return json.dumps({"timestamp": "2026-08-09T10:00:00Z", "type": t,
                       "payload": payload}, ensure_ascii=False)


def _codex_injected_user():
    """role=user 伪装注入 (实测 65% 形态) — 绝不可进用户语料。"""
    return _codex_line(
        "response_item", type="message", role="user",
        content=[{"type": "input_text",
                  "text": "# AGENTS.md instructions for /w/proj\n\n"
                          "<INSTRUCTIONS>\n## Skills\n技能投影说明\n</INSTRUCTIONS>"}])


def _mk_codex(pdir, name, meta_cwd, sid="0199dd9c-ddea-7662-95a7-7b3d6feacd39",
              extra_lines=()):
    d = pdir / "2026" / "08" / "09"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    lines = [
        json.dumps({"timestamp": "2026-08-09T10:00:00Z", "type": "session_meta",
                    "payload": {"id": sid, "cwd": meta_cwd,
                                "cli_version": "0.98.0"}},
                   ensure_ascii=False),
        _codex_injected_user(),
    ]
    lines.extend(extra_lines)
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def test_codex_locate_by_meta_cwd_and_session_id(tmp_path, monkeypatch):
    """无项目目录结构 → locate 按会话头 session_meta.cwd 结构匹配过滤。"""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    pdir = tmp_path / "home" / ".codex" / "sessions"
    f_hit = _mk_codex(pdir, "rollout-2026-08-09T10-00-00-aaaa.jsonl",
                      "/w/proj", sid="0199dd9c-ddea-7662-95a7-7b3d6feacd39")
    _mk_codex(pdir, "rollout-2026-08-09T11-00-00-bbbb.jsonl",
              "/other/place", sid="0199dd9c-ffff-7662-95a7-7b3d6feacd39")
    got = transcripts.locate("/w/proj", "codex")
    assert got == [f_hit]  # cwd 不匹配的会话被结构过滤
    assert transcripts.session_id(f_hit, "codex") == \
        "0199dd9c-ddea-7662-95a7-7b3d6feacd39"  # 会话头 id, 非文件名


def test_codex_end_steps_and_scenes(tmp_path):
    """end step = response_item assistant output_text (无 stop_reason 语义);
    用户语料只来自 event_msg/user_message — role=user 注入绝不混入。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "rollout-x.jsonl"
        ev_real1 = _codex_line("event_msg", type="user_message",
                               message="提取这个备份包", kind="plain")
        ev_real2 = _codex_line("event_msg", type="user_message",
                               message="放到外置盘根目录", kind="plain")
        asst_long = _codex_line(
            "response_item", type="message", role="assistant",
            content=[{"type": "output_text",
                      "text": LONG + "\n补充: 解包校验通过。"}])
        extra = [
            ev_real1, ev_real2,
            _codex_line("response_item", type="message", role="assistant",
                        content=[{"type": "output_text", "text": SHORT}]),  # 门
            asst_long,
            _codex_line("response_item", type="message", role="assistant",
                        content=[{"type": "output_text", "text": LONG + "\n补充: 解包校验通过。"}]),  # 去重
        ]
        f = _mk_codex(Path(td), "rollout-x.jsonl", "/w/proj",
                      extra_lines=extra)
        assert transcripts.end_steps(f, "codex") == [LONG + "\n补充: 解包校验通过。"]
        sc = transcripts.scenes(f, "codex")
        assert len(sc) == 1
        assert sc[0]["end_step"] == LONG + "\n补充: 解包校验通过。"
        # 用户语料 = event_msg 真人输入; 注入块不在
        assert sc[0]["user_blocks"] == ["提取这个备份包", "放到外置盘根目录"]
        assert all("技能投影说明" not in b for b in sc[0]["user_blocks"])


def test_codex_facade_clean_strips_injected_echo(tmp_path):
    """assistant 回显注入块 → facade 清洗口剥 (corpus_prep codex 规则)。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        extra = [
            _codex_line("response_item", type="message", role="assistant",
                        content=[{"type": "output_text", "text":
                                  "<environment_context>\n<cwd>/x</cwd>\n"
                                  "</environment_context>\n" + LONG}]),
        ]
        f = _mk_codex(Path(td), "rollout-y.jsonl", "/w/proj",
                      extra_lines=extra)
        assert transcripts.end_steps(f, "codex") == [LONG]


def test_codex_exec_session_zero_user_voice(tmp_path):
    """SDK/exec 型会话无 event_msg → 用户语料自然为零 (无真人交互)。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        extra = [
            _codex_line("response_item", type="message", role="user",
                        content=[{"type": "input_text",
                                  "text": "执行某任务的指令文本足够长吗并不"}]),
            _codex_line("response_item", type="message", role="assistant",
                        content=[{"type": "output_text", "text": LONG}]),
        ]
        f = _mk_codex(Path(td), "rollout-z.jsonl", "/w/proj",
                      extra_lines=extra)
        sc = transcripts.scenes(f, "codex")
        assert len(sc) == 1 and sc[0]["user_blocks"] == []
