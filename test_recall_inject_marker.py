"""UserPromptSubmit 注入器出端打标验收 (2026-08-28 出端闭环)。

覆盖:
1. 命中注入 → additionalContext 整体包 <memsvc-recall> 标记块 (LLM 可读,
   harness 不解释=零适配器), 且该 ctx 被 corpus_prep 五 harness 整块剥净
   (闭环: 打标面 = 清洗面); v1.7⑤a 全降级块加 quality="fallback" 属性 +
   块顶警示, 属性被清洗面通配吸收。
2. prompt 未指名已知实体 → 零输出 (既有精度门, 回归锚)。
3. 预算扣除包裹开销后条目照常注入 (MAX_BYTES 承诺不破)。

零网络零 LLM (cli.recall / search_entities / LIF 记账全 monkeypatch)。
"""
import io
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "hooks"))

import recall_inject as ri


def _payload(prompt="专家职位 的结论是什么"):
    return json.dumps({"prompt": prompt, "session_id": "s1",
                       "cwd": "/tmp/fake-proj"})


def _readonly_conn(*a, **k):
    """连接失败 raise 形态 (先例 test_recall_inject_rw_degrade.py:105-108)。

    A1-RW-001-F1: 弃 `get_conn→lambda:None` 裸夹具 — None 会沿
    `conn or db.get_conn()` 流入 `.execute` 产生误导性的
    AttributeError(NoneType), 正是 A1 台账误诊的根源。"""
    raise sqlite3.OperationalError("attempt to write a readonly database")


def _patch_recall(monkeypatch, n_hits=2, max_bytes="4096"):
    import cli
    import db
    import recall as recall_mod
    import scoring
    monkeypatch.setenv("MEM_RECALL_MIN_SCORE", "0.05")
    monkeypatch.setenv("MEM_RECALL_MAX_BYTES", max_bytes)
    monkeypatch.setattr(recall_mod, "search_entities",
                        lambda toks: [{"id": "e1", "name": "专家职位"}])
    hits = [{"score": 0.42, "tag": {"display": "专家职位"},
             "fact": {"id": f"f{i}", "subject_id": "e1", "object_id": None,
                      "value": f"结论 {i}"}}
            for i in range(n_hits)]
    monkeypatch.setattr(cli, "recall", lambda *a, **k: {"results": hits})
    # A1-RW-001-F1: 日志路径隔离 — 注入器任何台账写入落 capture, 绝不污染
    # 真实 data/hook-recall.log (历史: pytest 写入的行曾被误读为桥 spawn 故障)。
    logged: list[str] = []
    monkeypatch.setattr(ri, "_log_fail", logged.append)
    monkeypatch.setattr(db, "get_conn", _readonly_conn)
    monkeypatch.setattr(scoring, "refresh_lif_on_recall", lambda *a, **k: None)


def test_additional_context_wrapped_in_memsvc_recall_marker(monkeypatch):
    _patch_recall(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO(_payload()))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert ri.main() == 0
    ctx = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
    # v1.7⑤a: stub facts 未带 extractor 键 = 保守按 fallback 档 (extractor
    # 缺省即 regex 档, 与 fact 表列默认一致) → 全降级块打 quality 属性 +
    # 块顶警示; 包裹不变式仍以 <memsvc-recall 开头、</memsvc-recall> 结尾。
    assert ctx.startswith('<memsvc-recall quality="fallback">')
    assert ctx.endswith("</memsvc-recall>")
    assert "[warning]" in ctx and "未经主径 LLM 验证" in ctx
    assert "## Memory recall (auto, 2 hits)" in ctx
    assert "结论 0" in ctx and "[0.42]" in ctx
    # 闭环: 打标面 = 清洗面 — 五 harness 剥完都是空 (语料重进不重入库;
    # quality 属性被 corpus_prep 开标签通配吸收, 清洗面零改动)
    from corpus_prep import HARNESSES, clean
    for h in HARNESSES:
        assert clean(ctx, h) == "", h


def test_no_anchor_entities_zero_output(monkeypatch):
    import recall as recall_mod
    monkeypatch.setattr(sys, "stdin",
                        io.StringIO(_payload("完全无关的提问内容")))
    monkeypatch.setattr(recall_mod, "search_entities",
                        lambda toks: [{"id": "e1", "name": "专家职位"}])
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert ri.main() == 0
    assert out.getvalue() == ""  # 精度门: 未指名实体 → 零注入


def test_budget_still_injects_after_wrap_overhead(monkeypatch):
    _patch_recall(monkeypatch, n_hits=1, max_bytes="1024")
    monkeypatch.setattr(sys, "stdin", io.StringIO(_payload()))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert ri.main() == 0
    raw = out.getvalue()
    assert raw  # 预算远大于包裹开销 32B → 照常注入
    ctx = json.loads(raw)["hookSpecificOutput"]["additionalContext"]
    assert len(ctx.encode("utf-8")) <= 1024
