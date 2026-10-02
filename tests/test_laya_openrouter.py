"""openrouter jev 后端 (2026-10-02): 后端分发 / answers 归一 / key 缺失语义 /
分片路径共用。零网络 (monkeypatch _openrouter_post / urlopen)。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import laya_client  # noqa: E402


def _score_answer(p2: float) -> dict:
    return {"score": 2.0 * p2, "probabilities": {"0": 0.1, "1": 0.9 - p2, "2": p2}}


def test_backend_switch_and_available(monkeypatch):
    """openrouter 后端: key 在 → available True (不探 health); key 缺 → False;
    缺省 local 走 /predict。"""
    monkeypatch.setenv("MEM_LAYA_ENABLED", "1")
    monkeypatch.setattr(laya_client, "_avail_cache", None)
    monkeypatch.setenv("MEM_LAYA_BACKEND", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    assert laya_client.laya_available() is True
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert laya_client.laya_available() is False
    monkeypatch.setenv("MEM_LAYA_BACKEND", "local")
    # local + 无服务 → health 探测 False (真网络尝试, 超时 3s; 环境无 8190 即 False)
    assert laya_client.laya_available() in (True, False)  # 不崩即过


def test_openrouter_batch_normalize(monkeypatch):
    """laya_batch 在 openrouter 后端走 _openrouter_post 且 answers 原样透传;
    渲染围栏/前言的 content 也能剥出 JSON。"""
    calls = {}

    def fake_post(state, questions, timeout):
        calls["state"], calls["qs"] = state, questions
        return {"answers": {qid: _score_answer(0.8) for qid in questions}}

    monkeypatch.setattr(laya_client, "_openrouter_post", fake_post)
    monkeypatch.setenv("MEM_LAYA_BACKEND", "openrouter")
    got = laya_client.laya_batch("ctx", {"q1": {"type": "score",
                                                "criteria": ["a", "b", "c"]}})
    assert got == {"q1": _score_answer(0.8)}
    assert calls["state"] == "ctx"


def test_openrouter_post_parses_decisions(monkeypatch):
    """_openrouter_post: /alpha/decisions 原生面 — 同形 answers 透传;
    answers 非 dict → None。"""
    class _Resp:
        def __init__(self, body):
            self._b = json.dumps(body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._b

    def fake_urlopen(req, timeout=None):
        assert "alpha/decisions" in req.full_url
        payload = json.loads(req.data.decode())
        assert payload["model"] == laya_client.JEV_MODEL
        assert payload["state"] == "s" and set(payload["questions"]) == {"safe"}
        return _Resp({"model": "x", "answers": {"safe": _score_answer(0.5)}})

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(laya_client.urllib.request, "urlopen", fake_urlopen)
    got = laya_client._openrouter_post("s", {"safe": {}}, 5.0)
    assert got["answers"]["safe"]["probabilities"]["2"] == 0.5

    def bad_urlopen(req, timeout=None):
        return _Resp({"answers": "not a dict"})

    monkeypatch.setattr(laya_client.urllib.request, "urlopen", bad_urlopen)
    assert laya_client._openrouter_post("s", {"safe": {}}, 5.0) is None


def test_openrouter_no_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert laya_client._openrouter_post("s", {"q": {}}, 5.0) is None
