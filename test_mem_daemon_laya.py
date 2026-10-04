"""T2 laya: mem_daemon 路径判官注入锚 + H1 spool 第二 watch 源段级消费。

daemon 两处 autodream 调用 (sweep + trigger) 须与 cli.autodream 同构 —
providers 头部换 LayaJudgeProvider; 开关关 → [] (autodream 对 None/[] 等价,
autodream.py ``list(providers) if providers else []``)。

H1 (2026-10-01, spec v2 §二): data/transcript-spool 段级消费 — 新文件消费/
已见 sha 跳过/laya 挂起重试/段级 ack/毒段 DLQ/硬超时。全程 tmp spool
(MEM_SPOOL_DIR 注入) + sys.modules 假 distill + _sha_seen monkeypatch —
生产 db/spool 零触碰, 零 LLM。
"""

import json
import pathlib
import sys
import time
import types

import pytest

import mem_daemon


@pytest.fixture
def capture_autodream(monkeypatch, tmp_path):
    """抓 daemon 路径 autodream 收到的 providers。"""
    calls = {}

    def fake_autodream(sid, tpath, providers=None, **kw):
        calls["providers"] = providers
        return {"added": 0, "updated": 0, "deleted": 0, "noop": 0}

    monkeypatch.setattr(mem_daemon.autodream_mod, "autodream", fake_autodream)
    return calls, tmp_path


def _write_jsonl(path):
    # 一行须 > GROWTH_THRESHOLD(512B) 才触发 dream
    line = {"type": "user", "message": {"content": "x" * 600}, "sessionId": "s1"}
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")


def test_sweep_path_laya_judge_injected(capture_autodream, monkeypatch):
    calls, tmp_path = capture_autodream
    _write_jsonl(tmp_path / "t.jsonl")
    monkeypatch.setenv("MEM_LAYA_ENABLED", "1")
    monkeypatch.setattr("laya_client.laya_available", lambda: True)
    mem_daemon._sweep(tmp_path, "/w", {})
    from llm_provider import LayaJudgeProvider
    assert isinstance(calls["providers"][0], LayaJudgeProvider)


def test_sweep_path_laya_off_empty_providers(capture_autodream, monkeypatch):
    calls, tmp_path = capture_autodream
    _write_jsonl(tmp_path / "t.jsonl")
    monkeypatch.setenv("MEM_LAYA_ENABLED", "0")
    mem_daemon._sweep(tmp_path, "/w", {})
    assert calls["providers"] == []


def test_trigger_path_laya_judge_injected(capture_autodream, monkeypatch):
    calls, tmp_path = capture_autodream
    jf = tmp_path / "t.jsonl"
    _write_jsonl(jf)
    monkeypatch.setattr(mem_daemon, "TRIGGER_FILE", tmp_path / "trigger.json")
    (tmp_path / "trigger.json").write_text(json.dumps({
        "session_id": "s1", "transcript_path": str(jf), "cwd": "/w"}))
    monkeypatch.setenv("MEM_LAYA_ENABLED", "1")
    monkeypatch.setattr("laya_client.laya_available", lambda: True)
    mem_daemon._check_trigger({}, "/w")
    from llm_provider import LayaJudgeProvider
    assert isinstance(calls["providers"][0], LayaJudgeProvider)


# ── H1: spool 第二 watch 源 (段级消费) ──────────────────────────────

def _fake_distill(monkeypatch, segment_fn, audit=lambda: 0,
                  reembed=lambda: 0):
    """sys.modules 注入假 distill (上游契约: distill_segment/LayaUnavailable/
    audit_pending/reembed_needing)。"""
    mod = types.ModuleType("distill")
    mod.LayaUnavailable = type("LayaUnavailable", (Exception,), {})
    mod.distill_segment = segment_fn
    mod.audit_pending = audit
    mod.reembed_needing = reembed
    monkeypatch.setitem(sys.modules, "distill", mod)
    return mod


def _spool(tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir(exist_ok=True)
    return spool


def _cc_snapshot(spool, name, user="帮我总结这轮结论",
                 assistant="结论: 记忆管线 spool 段级消费路径回归验证。" * 8):
    """CC 格式快照: user + assistant end_turn (带行内 timestamp)。"""
    lines = [
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": user}]}},
        {"type": "assistant", "timestamp": "2026-10-01T10:00:00+00:00",
         "message": {"role": "assistant", "stop_reason": "end_turn",
                     "content": [{"type": "text", "text": assistant}]}},
    ]
    p = pathlib.Path(spool) / name
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n",
                 encoding="utf-8")
    return p


def _cc_two_seg_snapshot(spool, name):
    """CC 双段快照 (段级 ack 用): 两对 user+end_turn。"""
    lines = []
    for i in (1, 2):
        lines.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": f"第{i}问"}]}})
        lines.append({
            "type": "assistant",
            "timestamp": f"2026-10-01T10:0{i}:00+00:00",
            "message": {"role": "assistant", "stop_reason": "end_turn",
                        "content": [{"type": "text",
                                     "text": f"第{i}结论句输出。" * 10}]}})
    p = pathlib.Path(spool) / name
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n",
                 encoding="utf-8")
    return p


def _dsh_snapshot(spool, name, depth=0):
    """dsh 事件流快照: user/message + assistant/message + turn/end completed。"""
    lines = [
        {"type": "session", "delegationDepth": depth},
        {"type": "user/message", "data": {"message": {"content": [
            {"type": "text", "text": "dsh 用户提问"}]}}},
        {"type": "assistant/message", "data": {"message": {"content": [
            {"type": "text", "text": "dsh 结论句输出。" * 12}]}}},
        {"type": "turn/end", "data": {"reason": {"kind": "completed"}}},
    ]
    p = pathlib.Path(spool) / name
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n",
                 encoding="utf-8")
    return p


def test_spool_new_file_consumed_and_deleted(tmp_path, monkeypatch):
    """新快照 → 轮切 segment → distill_segment → 全段成功删文件 (旧 worker
    语义但仅成功后); session/cwd/ts/段文本 逐参透传。"""
    monkeypatch.setenv("MEM_SPOOL_DIR", str(_spool(tmp_path)))
    monkeypatch.setattr(mem_daemon, "_sha_seen", lambda sha: False)
    calls = []

    def seg(text, sid, cwd, ts):
        calls.append((text, sid, cwd, ts))
        return {"atoms": 2, "edges": 1, "merged": 0,
                "supersede_proposals": []}

    _fake_distill(monkeypatch, seg)
    f = _cc_snapshot(_spool(tmp_path), "sess-abc123def4567890.jsonl")
    state = mem_daemon._sweep_spool({}, "/watch/cwd")
    assert not f.exists(), "成功后未删 spool 文件"
    assert len(calls) == 1
    text, sid, cwd, ts = calls[0]
    assert sid == "sess"                      # 文件名剥尾段 sha16 (旧规则)
    assert cwd == "/watch/cwd"
    assert ts == "2026-10-01T10:00:00+00:00"  # CC 行 timestamp 透传
    assert text.startswith("[用户] 帮我总结") and "[助手] 结论:" in text
    assert state == {}, "消费完须清 state 键"


def test_spool_seen_sha_skipped_and_lock_recovered(tmp_path, monkeypatch):
    """已见 sha 零 LLM 跳过 (跨文件重放免疫); 旧 worker 遗留 .lock 复活消费。"""
    monkeypatch.setenv("MEM_SPOOL_DIR", str(_spool(tmp_path)))
    monkeypatch.setattr(mem_daemon, "_sha_seen", lambda sha: True)
    calls = []
    _fake_distill(monkeypatch, lambda *a: calls.append(a) or {})
    spool = _spool(tmp_path)
    f = _cc_snapshot(spool, "sess-seen-0000000000000001.jsonl")
    stale = _cc_snapshot(spool, "sess-stale-0000000000000002.jsonl")
    stale.rename(spool / "sess-stale-0000000000000002.jsonl.lock")
    state = mem_daemon._sweep_spool({}, "/w")
    assert calls == [], "已见 sha 须零 LLM 调用"
    assert not f.exists() and not list(spool.glob("*.jsonl.lock")), \
        "全已见=消费成功删文件; .lock 复活后被同轮消费"
    assert state == {}


def test_spool_laya_unavailable_suspends_then_resumes(tmp_path, monkeypatch):
    """LayaUnavailable → 挂起: 不删文件/offset 不动/不计失败; 恢复后补审。"""
    monkeypatch.setenv("MEM_SPOOL_DIR", str(_spool(tmp_path)))
    monkeypatch.setattr(mem_daemon, "_sha_seen", lambda sha: False)
    mode = {"down": True}
    calls = []

    def seg(*a):
        if mode["down"]:
            raise mod.LayaUnavailable("laya down")
        calls.append(a)
        return {"atoms": 1, "edges": 0, "merged": 0,
                "supersede_proposals": []}

    mod = _fake_distill(monkeypatch, seg)
    f = _cc_snapshot(_spool(tmp_path), "sess-laya-0000000000000003.jsonl")
    state = mem_daemon._sweep_spool({}, "/w")
    assert f.exists(), "挂起不删文件 (spool 积压持有)"
    key = next(k for k in state if k.startswith("spool:"))
    assert state[key]["offset"] == 0 and state[key]["attempts"] == 0, \
        "挂起不计失败次数, offset 不动"
    mode["down"] = False
    state = mem_daemon._sweep_spool(state, "/w")
    assert not f.exists() and len(calls) == 1, "恢复后补审且只补一次"
    assert state == {}


def test_spool_segment_ack_resumes_without_redistill(tmp_path, monkeypatch):
    """段级 ack: 首段成功 offset 推进, 恢复后只补未 ack 段 (不重蒸馏)。"""
    monkeypatch.setenv("MEM_SPOOL_DIR", str(_spool(tmp_path)))
    monkeypatch.setattr(mem_daemon, "_sha_seen", lambda sha: False)
    calls = []

    def seg(*a):
        calls.append(a)
        if len(calls) == 2:
            raise mod.LayaUnavailable("down on 2nd")
        return {"atoms": 1, "edges": 0, "merged": 0,
                "supersede_proposals": []}

    mod = _fake_distill(monkeypatch, seg)
    f = _cc_two_seg_snapshot(_spool(tmp_path), "sess-ack-0000000000000008.jsonl")
    state = mem_daemon._sweep_spool({}, "/w")
    key = next(k for k in state if k.startswith("spool:"))
    assert state[key]["offset"] > 0, "首段成功后 offset 须推进"
    assert f.exists()
    state = mem_daemon._sweep_spool(state, "/w")   # laya 恢复
    # sweep1: 第1段成功+第2段挂起 (2 调); 恢复轮只补第2段 (第3 调),
    # 第1段已 ack 不重蒸馏。
    assert len(calls) == 3
    assert "第1结论" not in calls[2][0] and "第2结论" in calls[2][0], \
        "恢复后只补未 ack 段"
    assert not f.exists() and state == {}


def test_spool_poison_dlq_after_max_attempts(tmp_path, monkeypatch):
    """段失败 (非 laya) max-attempts=3 → 文件+sidecar 移 <spool>.dlq + state 清键。"""
    monkeypatch.setenv("MEM_SPOOL_DIR", str(_spool(tmp_path)))
    monkeypatch.setattr(mem_daemon, "_sha_seen", lambda sha: False)

    def seg(*a):
        raise RuntimeError("zhipu boom")

    _fake_distill(monkeypatch, seg)
    spool = _spool(tmp_path)
    f = _cc_snapshot(spool, "sess-poison-0000000000000004.jsonl")
    (spool / (f.name + ".harness")).write_text("cc", encoding="utf-8")
    state = {}
    for i in range(mem_daemon._SEGMENT_ATTEMPTS_MAX):
        state = mem_daemon._sweep_spool(state, "/w")
        if i < mem_daemon._SEGMENT_ATTEMPTS_MAX - 1:
            assert f.exists(), "未达次数帽须留文件重试"
    dlq = pathlib.Path(str(spool) + ".dlq")
    assert (dlq / f.name).is_file() and (dlq / (f.name + ".harness")).is_file()
    assert not f.exists() and state == {}


def test_spool_dsh_format_consumed_and_sidechain_skipped(tmp_path, monkeypatch):
    """dsh 事件流同轮消费 (user/message+turn/end); delegationDepth>0 侧链
    整文件排除, 零段=消费成功删文件 (旧 worker 同语义)。"""
    monkeypatch.setenv("MEM_SPOOL_DIR", str(_spool(tmp_path)))
    monkeypatch.setattr(mem_daemon, "_sha_seen", lambda sha: False)
    calls = []
    _fake_distill(monkeypatch, lambda *a: calls.append(a) or {})
    spool = _spool(tmp_path)
    main = _dsh_snapshot(spool, "sess-dsh-0000000000000005.jsonl")
    side = _dsh_snapshot(spool, "sess-side-0000000000000006.jsonl", depth=1)
    state = mem_daemon._sweep_spool({}, "/w")
    assert len(calls) == 1, "主链一段, 侧链零调用"
    assert "[用户] dsh 用户提问" in calls[0][0]
    assert "[助手] dsh 结论" in calls[0][0]
    assert not main.exists() and not side.exists()
    assert state == {}


def test_spool_segment_hard_timeout_counts_attempt(tmp_path, monkeypatch):
    """线程池硬超时: 超时段计失败留存文件 (不阻塞主循环, 走毒段 attempts 路径)。"""
    monkeypatch.setenv("MEM_SPOOL_DIR", str(_spool(tmp_path)))
    monkeypatch.setattr(mem_daemon, "_sha_seen", lambda sha: False)
    monkeypatch.setattr(mem_daemon, "SEGMENT_HARD_TIMEOUT", 0.2)

    def seg(*a):
        time.sleep(0.8)
        return {}

    _fake_distill(monkeypatch, seg)
    f = _cc_snapshot(_spool(tmp_path), "sess-slow-0000000000000007.jsonl")
    state = mem_daemon._sweep_spool({}, "/w")
    assert f.exists(), "硬超时不删文件"
    key = next(k for k in state if k.startswith("spool:"))
    assert state[key]["attempts"] == 1, "超时计一次失败"


def test_spool_zombie_inflight_skip_no_double_exec(tmp_path, monkeypatch):
    """M1 在途守卫: 上轮硬超时僵尸线程仍在跑同段 → 重试轮跳过不计 attempt
    (防双执行双入图); 僵尸退出后按 sha 去重正常续走。"""
    monkeypatch.setenv("MEM_SPOOL_DIR", str(_spool(tmp_path)))
    monkeypatch.setattr(mem_daemon, "_sha_seen", lambda sha: False)
    monkeypatch.setattr(mem_daemon, "SEGMENT_HARD_TIMEOUT", 0.1)
    calls = []

    def seg(*a):
        calls.append(a)
        time.sleep(0.6)
        return {}

    _fake_distill(monkeypatch, seg)
    f = _cc_snapshot(_spool(tmp_path), "sess-zomb-0000000000000009.jsonl")
    state = mem_daemon._sweep_spool({}, "/w")
    key = next(k for k in state if k.startswith("spool:"))
    assert state[key]["attempts"] == 1
    # 僵尸线程 (sleep 未完) 仍在途: 第二轮跳过 — 不计 attempt、不再提交执行。
    state = mem_daemon._sweep_spool(state, "/w")
    assert state[key]["attempts"] == 1, "在途段本轮跳过不计 attempt"
    assert len(calls) == 1, "僵尸未退出前不得重试同段 (双入图竞态)"
    assert f.exists()


def test_spool_ack_offset_absolute_on_resume(tmp_path, monkeypatch):
    """m1: 段级 ack = 本轮 base offset + 段尾相对偏移 — 恢复轮 (offset>0)
    不得把相对值当绝对值写回 (offset 回退 = 重切段重蒸馏)。"""
    monkeypatch.setenv("MEM_SPOOL_DIR", str(_spool(tmp_path)))
    monkeypatch.setattr(mem_daemon, "_sha_seen", lambda sha: False)
    lines = []
    for i, size in ((1, 3000), (2, 40), (3, 40)):   # 首段大, 后两段小
        lines.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": f"第{i}问"}]}})
        lines.append({
            "type": "assistant", "timestamp": f"2026-10-01T10:0{i}:00+00:00",
            "message": {"role": "assistant", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "句。" * size}]}})
    spool = _spool(tmp_path)
    f = spool / "sess-off-000000000000000b.jsonl"
    f.write_text("\n".join(json.dumps(l) for l in lines) + "\n",
                 encoding="utf-8")
    calls = []

    def seg(*a):
        calls.append(a)
        if len(calls) in (2, 4):        # 轮1的seg2 / 轮2的seg3 挂起
            raise mod.LayaUnavailable("down")
        return {"atoms": 1, "edges": 0, "merged": 0,
                "supersede_proposals": []}

    mod = _fake_distill(monkeypatch, seg)
    state1 = mem_daemon._sweep_spool({}, "/w")
    key = next(k for k in state1 if k.startswith("spool:"))
    off1 = state1[key]["offset"]
    assert off1 > 3000, "首段 (大段) 后 offset 须为绝对字节位"
    state2 = mem_daemon._sweep_spool(state1, "/w")
    assert state2[key]["offset"] > off1, \
        "m1: 恢复轮 ack 须 base+相对绝对偏移, 不得回退到相对值"


def test_spool_segment_cwd_from_cc_line(tmp_path, monkeypatch):
    """M5: CC transcript 行内顶层 cwd → distill 收段级真实 cwd (repo: 铸币
    依据); 无行内 cwd → watch 目录兜底 (dsh sidecar 只记 harness 名)。"""
    monkeypatch.setenv("MEM_SPOOL_DIR", str(_spool(tmp_path)))
    monkeypatch.setattr(mem_daemon, "_sha_seen", lambda sha: False)
    calls = []
    _fake_distill(monkeypatch, lambda *a: calls.append(a) or {})
    lines = [
        {"type": "user", "cwd": "/home/yy/projects/alpha",
         "message": {"role": "user", "content": [{"type": "text", "text": "问"}]}},
        {"type": "assistant", "cwd": "/home/yy/projects/alpha",
         "timestamp": "2026-10-01T11:00:00+00:00",
         "message": {"role": "assistant", "stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "结论句。" * 20}]}},
    ]
    p = _spool(tmp_path) / "sess-cwd-000000000000000a.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n",
                 encoding="utf-8")
    mem_daemon._sweep_spool({}, "/watch/default")
    assert calls[0][2] == "/home/yy/projects/alpha", \
        "段级 cwd 须取行内真实工作目录, 非 daemon 启动目录"


def test_maybe_dream_runs_distill_audit_and_reembed(monkeypatch):
    """H7 夜间补扫: dream 轮内补审 (audit_pending) + 补向量 (reembed_needing)。"""
    counters = {"audit": 0, "reembed": 0}
    _fake_distill(
        monkeypatch, lambda *a: {},
        audit=lambda: counters.__setitem__(
            "audit", counters["audit"] + 1) or 0,
        reembed=lambda: counters.__setitem__(
            "reembed", counters["reembed"] + 1) or 0)
    fake_dream = types.ModuleType("dream")
    fake_dream.run_cycle = lambda source_cwd: {"ok": 1}
    monkeypatch.setitem(sys.modules, "dream", fake_dream)
    monkeypatch.setattr(mem_daemon, "_run_hygiene", lambda state, cwd: state)
    state = mem_daemon._maybe_dream({}, "/w")
    assert counters == {"audit": 1, "reembed": 1}
    assert state["_dreaming"]["last_run"] > 0
