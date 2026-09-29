"""T0/P0 laya_client 验收锚(docs/specs/laya-integration-tickets.md)."""
import json

import pytest

import laya_client as lc


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    monkeypatch.setattr(lc, "_avail_cache", None)
    monkeypatch.setenv("MEM_LAYA_ENABLED", "1")
    yield


# ── norm_score ──

def test_norm_score():
    assert lc.norm_score({"score": 2.0}, 3) == 1.0
    assert lc.norm_score({"score": 3.0}, 4) == 1.0
    assert lc.norm_score({"score": 0.0}, 2) == 0.0


# ── laya_batch: 透传 / 分片 / 失败矩阵 ──

class FakeResp:
    def __init__(self, payload):
        self._payload, self.status = payload, 200

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_batch_passthrough(monkeypatch):
    calls = []

    def fake_post(path, payload, timeout):
        calls.append(payload)
        return FakeResp({"answers": {"q1": {"score": 1.0}}})._payload

    monkeypatch.setattr(lc, "_post", fake_post)
    out = lc.laya_batch("s", {"q1": {"type": "noul"}})
    assert out == {"q1": {"score": 1.0}}
    assert len(calls) == 1


def test_batch_sharding(monkeypatch):
    calls = []

    def fake_post(path, payload, timeout):
        calls.append(payload)
        return {"answers": {q: {"score": 0.5} for q in payload["questions"]}}

    monkeypatch.setattr(lc, "_post", fake_post)
    state = "x" * 1000
    questions = {f"q{i}": {"type": "score", "criteria": ["low", "med", "high"],
                           "instructions": "r" * 8000}
                 for i in range(4)}  # 每问 ~1000 token → 4 问+state > 8000
    out = lc.laya_batch(state, questions)
    assert len(calls) >= 2
    assert all(c["state"] == state for c in calls)  # state 原样复制到每片
    assert out is not None and len(out) == 4


def test_batch_failure_matrix(monkeypatch):
    for fail in (lambda p, pl, t: None,                       # HTTP/超时(_post → None)
                 lambda p, pl, t: {"nope": 1}):               # 无 answers 键
        monkeypatch.setattr(lc, "_post", fail)
        assert lc.laya_batch("s", {"q": {"type": "noul"}}) is None


# ── laya_available: TTL 缓存 / 失败缓存 / 开关短路 ──

def test_available_ttl_cache(monkeypatch):
    probes = []

    class Resp(FakeResp):
        pass

    def fake_urlopen(url, timeout=None):
        probes.append(url)
        return Resp({})

    monkeypatch.setattr(lc.urllib.request, "urlopen", fake_urlopen)
    assert lc.laya_available() is True
    assert lc.laya_available() is True
    assert len(probes) == 1  # TTL 窗口内第二次零网络


def test_available_failure_cached(monkeypatch):
    probes = []

    def fake_urlopen(url, timeout=None):
        probes.append(url)
        raise OSError("down")

    monkeypatch.setattr(lc.urllib.request, "urlopen", fake_urlopen)
    assert lc.laya_available() is False
    assert lc.laya_available() is False
    assert len(probes) == 1
    # 失败不抛


def test_available_disabled_zero_network(monkeypatch):
    monkeypatch.setenv("MEM_LAYA_ENABLED", "0")

    def boom(url, timeout=None):
        raise AssertionError("must not hit network")

    monkeypatch.setattr(lc.urllib.request, "urlopen", boom)
    assert lc.laya_available() is False
