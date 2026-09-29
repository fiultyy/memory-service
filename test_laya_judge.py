"""laya T2 — LayaJudgeProvider 矛盾裁决位 (组合降级) 单测.

spec: docs/specs/laya-integration-tickets.md T2, docs/specs/laya-integration-spec-v1.1.md §二
"""
import pytest

import cli
import llm_provider as lp
from autodream import _judge_contradiction
from llm_provider import LayaJudgeProvider, ProviderCallError


class FakeFallback:
    """记录调用的假 fallback provider (judge 契约: {"contradiction": bool})。"""

    def __init__(self, result=None, raises=False):
        self.calls = []
        self.result = result or {"contradiction": False, "reason": "fb"}
        self.raises = raises

    def extract_facts(self, text):
        raise AssertionError("judge-only path should not extract")

    def dedupe_entity(self, new_name, new_type, candidates, context=None):
        raise AssertionError("judge-only path should not dedupe")

    def judge_contradiction(self, subject_type, subject_name, predicate,
                            new_value, old_value):
        self.calls.append((subject_type, subject_name, predicate,
                           new_value, old_value))
        if self.raises:
            raise ProviderCallError("fallback also down")
        return dict(self.result)


def _laya(monkeypatch, available=True, answer=None):
    """monkeypatch laya_client 三件套; answer=None → laya_batch 返回 None。"""
    import laya_client as lc
    batch_calls = []

    def fake_batch(state, questions, timeout=30.0):
        batch_calls.append((state, questions))
        if answer is None:
            return None
        return {qid: answer for qid in questions}

    monkeypatch.setattr(lc, "laya_available", lambda: available)
    monkeypatch.setattr(lc, "laya_batch", fake_batch)
    # llm_provider 里是函数内 import laya_client → 模块属性查找, patch 生效
    return batch_calls


# ── noul 0.5 / 0.49 两侧 ──

def test_noul_threshold_true(monkeypatch):
    _laya(monkeypatch, answer={"noul": 1.0})  # noul=1.0 >= 0.5
    p = LayaJudgeProvider()
    assert p.judge_contradiction("c", "x", "status", "new", "old") == \
        {"contradiction": True}


def test_noul_threshold_false(monkeypatch):
    _laya(monkeypatch, answer={"noul": 0.0})  # noul=0.0 < 0.5
    p = LayaJudgeProvider()
    assert p.judge_contradiction("c", "x", "status", "new", "old") == \
        {"contradiction": False}


def test_state_and_question_shape(monkeypatch):
    calls = _laya(monkeypatch, answer={"noul": 1.0})
    p = LayaJudgeProvider()
    p.judge_contradiction("tool", "memsvc", "lang", "rust", "python")
    state, questions = calls[0]
    assert "old: memsvc --lang--> python" in state
    assert "new: memsvc --lang--> rust" in state
    assert questions == {"supersede": {
        "type": "noul", "instructions": "Should old be superseded by new?"}}


# ── 降级矩阵 ──

def test_laya_down_fallback_passthrough(monkeypatch):
    _laya(monkeypatch, available=False)
    fb = FakeFallback(result={"contradiction": True, "reason": "fb"})
    p = LayaJudgeProvider(fallback=fb)
    out = p.judge_contradiction("tool", "memsvc", "lang", "rust", "python")
    assert out == {"contradiction": True, "reason": "fb"}
    # 透传参数逐位一致
    assert fb.calls == [("tool", "memsvc", "lang", "rust", "python")]


def test_batch_none_fallback(monkeypatch):
    _laya(monkeypatch, available=True, answer=None)  # 整批 None
    fb = FakeFallback()
    p = LayaJudgeProvider(fallback=fb)
    assert p.judge_contradiction("c", "x", "p", "n", "o") == \
        {"contradiction": False, "reason": "fb"}
    assert fb.calls


def test_both_down_provider_call_error(monkeypatch):
    _laya(monkeypatch, available=False)
    p = LayaJudgeProvider()  # fallback None
    with pytest.raises(ProviderCallError):
        p.judge_contradiction("c", "x", "p", "n", "o")


def test_fallback_also_raises(monkeypatch):
    _laya(monkeypatch, available=False)
    p = LayaJudgeProvider(fallback=FakeFallback(raises=True))
    with pytest.raises(ProviderCallError):
        p.judge_contradiction("c", "x", "p", "n", "o")


def test_outer_judge_catches_to_false(monkeypatch):
    # 外层 autodream._judge_contradiction 捕获 → False (supersede 不发生)
    _laya(monkeypatch, available=False)
    verdict = _judge_contradiction([LayaJudgeProvider()], "c", "x", "p", "n", "o")
    assert verdict is False


# ── multivalue / 同值快路径零网络 ──

def test_multivalue_shortcircuit_no_network(monkeypatch):
    calls = _laya(monkeypatch, answer={"noul": 1.0})
    verdict = _judge_contradiction(
        [LayaJudgeProvider()], "c", "x", "uses", "new", "old")
    assert verdict is False and calls == []


def test_same_value_shortcircuit_no_network(monkeypatch):
    calls = _laya(monkeypatch, answer={"noul": 1.0})
    verdict = _judge_contradiction(
        [LayaJudgeProvider()], "c", "x", "status", "same", "same")
    assert verdict is False and calls == []


# ── cli 注入位 ──

def test_cli_prefix_off_keeps_list(monkeypatch):
    import laya_client as lc
    monkeypatch.setenv("MEM_LAYA_ENABLED", "0")
    monkeypatch.setattr(lc, "_avail_cache", None)  # 清 TTL 缓存
    base = [object()]
    assert cli._laya_judge_prefix(base) == base


def test_cli_prefix_on(monkeypatch):
    _laya(monkeypatch, available=True)
    fb = object()
    out = cli._laya_judge_prefix([fb])
    assert len(out) == 1 and isinstance(out[0], LayaJudgeProvider)
    assert out[0].fallback is fb


def test_cli_prefix_on_empty_base(monkeypatch):
    _laya(monkeypatch, available=True)
    out = cli._laya_judge_prefix([])
    assert len(out) == 1 and isinstance(out[0], LayaJudgeProvider)
    assert out[0].fallback is None
