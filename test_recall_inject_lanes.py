"""v3 确定性 lane 注入测试 (hooks/recall_inject.py _det_lanes, 纯 SQL 零 LLM)。

覆盖:
1. preference lane 注入格式: 「偏好:」前缀, p_dur desc top3, valid_to 退场排除;
   与锚定召回同包 <memsvc-recall> 块且钉块首, 头部计数含 lane 行。
2. event lane 窗过滤: now-7d ~ now+21d 窗内才留, 距 now 最近优先,
   非 ISO 原文短语跳行 (不猜), 窗外/软删排除。
3. 无锚定实体 + lane 有货 → 纯 lane 注入 (旧: 零输出), 无 fallback 警示。
4. 两 lane 皆空 → 行为与现状一致 (无锚零输出)。
5. ~400B 总量帽: 超帽截行。

db.init(tmp) 隔离; cli.recall/search_entities/LIF 记账全 monkeypatch, 零网络。
"""
import io
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "hooks"))

import db
import recall_inject as ri


def _payload(prompt="专家职位 的结论是什么"):
    return json.dumps({"prompt": prompt, "session_id": "s1",
                       "cwd": "/tmp/fake-proj"})


def _atom(conn, text, *, label, p_dur=0.6, valid_to=None, event_at=None):
    cur = conn.execute(
        "INSERT INTO atom(text, label, p_dur, valid_from, valid_to, event_at) "
        "VALUES(?, ?, ?, '2026-01-01T00:00:00+00:00', ?, ?)",
        (text, label, p_dur, valid_to, event_at))
    return cur.lastrowid


def _patch(monkeypatch, n_hits=1):
    """锚定召回面全 mock (同 test_recall_inject_marker 邻近风格); db 留真
    (tmp 库) 供 _det_lanes 读 — LIF 记账面打空防伪 fact id 写 tmp 账。"""
    import cli
    import recall as recall_mod
    import scoring
    monkeypatch.setenv("MEM_RECALL_MIN_SCORE", "0.05")
    monkeypatch.setenv("MEM_RECALL_MAX_BYTES", "4096")
    monkeypatch.setattr(recall_mod, "search_entities",
                        lambda toks: [{"id": "e1", "name": "专家职位"}])
    hits = [{"score": 0.42, "tag": {"display": "专家职位"},
             "fact": {"id": f"f{i}", "subject_id": "e1", "object_id": None,
                      "extractor": "llm", "value": f"结论 {i}"}}
            for i in range(n_hits)]
    monkeypatch.setattr(cli, "recall", lambda *a, **k: {"results": hits})
    monkeypatch.setattr(ri, "_log_fail", lambda m: None)
    monkeypatch.setattr(scoring, "record_recall_observation", lambda *a, **k: None)
    monkeypatch.setattr(scoring, "refresh_lif_on_recall", lambda *a, **k: None)
    monkeypatch.setattr(scoring, "refresh_restricted", lambda f: True)


def _run(monkeypatch, prompt="专家职位 的结论是什么"):
    monkeypatch.setattr(sys, "stdin", io.StringIO(_payload(prompt)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert ri.main() == 0
    return out.getvalue()


def _ctx(raw):
    assert raw, "应有输出"
    return json.loads(raw)["hookSpecificOutput"]["additionalContext"]


def test_preference_lane_format_and_pin(monkeypatch, tmp_path):
    db.init(tmp_path / "m.db")
    conn = db.get_conn()
    _atom(conn, "回复用中文", label="preference", p_dur=0.3)
    _atom(conn, "ponytail 最短正确 diff", label="preference", p_dur=0.9)
    _atom(conn, "已退场偏好", label="preference", p_dur=1.0,
          valid_to="2026-09-01T00:00:00+00:00")  # 软删不入 lane
    _patch(monkeypatch)
    ctx = _ctx(_run(monkeypatch))
    # 「偏好:」前缀 + p_dur desc (0.9 在前) + 软删排除; 与锚定命中同包
    assert ctx.startswith("<memsvc-recall>") and ctx.endswith("</memsvc-recall>")
    i_hi, i_lo = ctx.find("偏好: ponytail"), ctx.find("偏好: 回复用中文")
    assert i_hi != -1 and i_lo != -1 and i_hi < i_lo, ctx
    assert "偏好: 已退场偏好" not in ctx
    # lane 钉块首: 先于锚定召回条目 (结论 0), 头部计数 = lane 2 + 锚定 1
    assert i_hi < ctx.find("- 专家职位 — 结论 0"), "lane 应钉块首"
    assert "## Memory recall (auto, 3 hits)" in ctx, ctx.splitlines()[1]
    # 回声闸覆盖: 五 harness 清洗面整块剥净
    from corpus_prep import HARNESSES, clean
    for h in HARNESSES:
        assert clean(ctx, h) == "", h


def test_event_lane_window_filter_and_order(monkeypatch, tmp_path):
    db.init(tmp_path / "m.db")
    conn = db.get_conn()
    now = datetime.now().astimezone()
    iso = lambda dt: dt.isoformat()  # noqa: E731 — 测试内联速记
    _atom(conn, "临近日程", label="event", event_at=iso(now + timedelta(days=2)))
    _atom(conn, "刚过期日程", label="event", event_at=iso(now - timedelta(days=6)))
    _atom(conn, "远未来", label="event", event_at=iso(now + timedelta(days=30)))
    _atom(conn, "远过去", label="event", event_at=iso(now - timedelta(days=10)))
    _atom(conn, "原文短语不猜", label="event", event_at="下周三下午")
    _atom(conn, "软删日程", label="event", event_at=iso(now + timedelta(days=1)),
          valid_to="2026-09-01T00:00:00+00:00")
    _patch(monkeypatch, n_hits=0)
    ctx = _ctx(_run(monkeypatch, prompt="完全无关提问"))  # 无锚 → 纯 lane 注入
    assert "日程: 临近日程" in ctx and "日程: 刚过期日程" in ctx, ctx
    # 窗外/非 ISO/软删全排除
    assert "远未来" not in ctx and "远过去" not in ctx
    assert "原文短语" not in ctx and "软删日程" not in ctx
    # 距 now 最近优先: 2d 未来 vs 6d 过去 → 临近日程在前
    assert ctx.find("日程: 临近日程") < ctx.find("日程: 刚过期日程")
    # 纯 lane 块无 fallback 警示 (确定性 SQL 产物不参与 fallback 判定)
    assert "quality=" not in ctx and "[warning]" not in ctx


def test_both_lanes_empty_behavior_unchanged(monkeypatch, tmp_path):
    db.init(tmp_path / "m.db")  # 空库: 两 lane 皆空
    import recall as recall_mod
    monkeypatch.setattr(recall_mod, "search_entities",
                        lambda toks: [{"id": "e1", "name": "专家职位"}])
    monkeypatch.setattr(ri, "_log_fail", lambda m: None)
    assert _run(monkeypatch, prompt="完全无关提问") == ""  # 无锚零输出 (现状)


def test_lane_budget_cap_truncates_rows(monkeypatch, tmp_path):
    db.init(tmp_path / "m.db")
    conn = db.get_conn()
    for i in range(3):  # 3 条长偏好 > 400B 帽 → 截行
        _atom(conn, f"偏好条款{i}_" + "长" * 100, label="preference", p_dur=0.5 + i * 0.1)
    _patch(monkeypatch, n_hits=0)
    ctx = _ctx(_run(monkeypatch, prompt="完全无关提问"))
    lane_rows = [ln for ln in ctx.splitlines() if ln.startswith("偏好:")]
    assert 0 < sum(len(ln.encode("utf-8")) for ln in lane_rows) \
        <= ri._LANE_MAX_BYTES
    assert len(lane_rows) == 1, lane_rows  # 两条即超帽 → 只留 p_dur 最高首行


if __name__ == "__main__":
    import tempfile
    import pytest
    for fn in (test_preference_lane_format_and_pin,
               test_event_lane_window_filter_and_order,
               test_both_lanes_empty_behavior_unchanged,
               test_lane_budget_cap_truncates_rows):
        with tempfile.TemporaryDirectory() as td, pytest.MonkeyPatch.context() as mp:
            fn(mp, Path(td))
        print(f"✓ {fn.__name__}")
    print("\n✓ All recall_inject lanes tests passed")
