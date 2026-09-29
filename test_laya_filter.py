"""T3/E1 laya 提取前过滤测试 (spec §三 / laya-integration-tickets T3)。

覆盖 _decide_segments 的 MEM_LAYA_FILTER 通道:
- 低分段 (noul<0.4) 不进 LLM 抽取、gazetteer 保底照跑; 高分段照常 LLM。
- 整批 None → 不过滤 (行为与开关关逐位一致)。
- MEM_LAYA_FILTER=0 / regex 档 → laya_batch 零调用; regex 档所有段照跑 gazetteer。

测试规范: def test_xxx() 函数让 pytest 收集 (项目头号雷区=模块级裸 assert)。
"""
from pathlib import Path
from types import SimpleNamespace

import autodream
import db
import gazetteer
import laya_client
import llm_extract

SEGS = ["噪声段甲纯寒暄", "结论段乙: 采纳斯普利特方案", "噪声段丙纯闲聊"]


def _stub(tag):
    return SimpleNamespace(entities=[], edges=[], confidence=0.9,
                           source_meta={"extractor_label": tag})


def _setup(tmp_path, monkeypatch, answers):
    """空产出 stub 通道 + mock laya_batch; 返回调用记录。"""
    db.init(Path(tmp_path) / "laya-filter.db")
    calls = {"llm": [], "gaz": [], "laya": 0}
    monkeypatch.setattr(
        llm_extract, "extract",
        lambda t: (calls["llm"].append(t), _stub("llm"))[1])
    monkeypatch.setattr(
        gazetteer, "extract",
        lambda t: (calls["gaz"].append(t), _stub("regex"))[1])

    def fake_batch(state, questions, timeout=30.0):
        calls["laya"] += 1
        calls["state"] = state
        calls["questions"] = questions
        return answers
    monkeypatch.setattr(laya_client, "laya_batch", fake_batch)
    monkeypatch.setattr(laya_client, "laya_available", lambda: True)
    return calls


def _decide(**kw):
    kw.setdefault("session_id", None)
    kw.setdefault("providers", [])
    kw.setdefault("fact_type", "stable")
    kw.setdefault("source_cwd", None)
    kw.setdefault("transcript_path", None)
    kw.setdefault("use_regex_channel", False)
    kw.setdefault("use_fallback_auto", False)
    kw.setdefault("allow_enqueue", False)
    return autodream._decide_segments(list(zip([None] * len(SEGS), SEGS)), **kw)


def test_low_score_segments_skip_llm(tmp_path, monkeypatch):
    """noul<0.4 段不调 llm_extract、gazetteer 保底照跑; 高分段照常 LLM。"""
    monkeypatch.setenv("MEM_LAYA_FILTER", "1")
    # noul 概率: seg_0 → 0.0 (跳), seg_1 → 0.9 (留), seg_2 → 0.4 (留, 边界上侧)
    calls = _setup(tmp_path, monkeypatch,
                   {"seg_0": {"noul": 0.0}, "seg_1": {"noul": 0.9},
                    "seg_2": {"noul": 0.4}})
    _decide()
    assert calls["laya"] == 1, "全段应恰一次批量 laya_batch"
    assert calls["llm"] == [SEGS[1], SEGS[2]], "低分段 seg_0 不得进 LLM 提取"
    assert calls["gaz"] == [SEGS[0]], "低分段走 gazetteer 保底"
    # state 含全部段文本; 每段恰 1 个 noul question
    assert all(t in calls["state"] for t in SEGS)
    assert set(calls["questions"]) == {"seg_0", "seg_1", "seg_2"}
    assert all(q["type"] == "noul" for q in calls["questions"].values())


def test_threshold_boundary_filters_below_040(tmp_path, monkeypatch):
    """noul 恰 <0.4 过滤、>=0.4 不过滤; 缺 noul 键的畸形段不过滤 (保守)。"""
    monkeypatch.setenv("MEM_LAYA_FILTER", "1")
    calls = _setup(tmp_path, monkeypatch,
                   {"seg_0": {"noul": 0.39}, "seg_1": {"noul": 0.4},
                    "seg_2": {}})  # seg_2 answer 畸形 → 不过滤
    _decide()
    assert calls["llm"] == [SEGS[1], SEGS[2]]
    assert calls["gaz"] == [SEGS[0]]


def test_batch_none_no_filter(tmp_path, monkeypatch):
    """整批 None → 全不过滤: 行为与开关关逐位一致 (llm 全调, gaz 零调)。"""
    monkeypatch.setenv("MEM_LAYA_FILTER", "1")
    calls = _setup(tmp_path, monkeypatch, None)
    _decide()
    assert calls["laya"] == 1
    assert calls["llm"] == SEGS
    assert calls["gaz"] == []


def test_filter_off_zero_laya_calls(tmp_path, monkeypatch):
    """MEM_LAYA_FILTER=0 → laya_batch 零调用, 行为=现状。"""
    monkeypatch.setenv("MEM_LAYA_FILTER", "0")
    calls = _setup(tmp_path, monkeypatch, {"seg_0": {"score": 0}})
    _decide()
    assert calls["laya"] == 0
    assert calls["llm"] == SEGS
    assert calls["gaz"] == []


def test_regex_channel_all_segments_unfiltered(tmp_path, monkeypatch):
    """regex 档: 无 LLM 调用可省 → 不触发 laya; 所有段照跑 gazetteer 不跳。"""
    monkeypatch.setenv("MEM_LAYA_FILTER", "1")
    calls = _setup(tmp_path, monkeypatch,
                   {"seg_0": {"score": 0}, "seg_1": {"score": 0},
                    "seg_2": {"score": 0}})
    _decide(use_regex_channel=True)
    assert calls["laya"] == 0, "regex 档不得触发 laya_batch"
    assert calls["gaz"] == SEGS, "regex 通道对所有段不跳"
    assert calls["llm"] == []
