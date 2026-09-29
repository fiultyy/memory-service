"""T2 laya: mem_daemon 路径判官注入锚。

daemon 两处 autodream 调用 (sweep + trigger) 须与 cli.autodream 同构 —
providers 头部换 LayaJudgeProvider; 开关关 → [] (autodream 对 None/[] 等价,
autodream.py ``list(providers) if providers else []``)。
"""

import json

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
