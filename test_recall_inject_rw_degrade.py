"""A1 读写降级测试 — hook 上下文 DB 只读时跳过 LIF 记账、注入照常 (2026-09-01 裁决)。

背景: dsh 桥拉起的 hook 进程内 sqlite 可能 `attempt to write a readonly database`
(疑会话沙箱/WAL 态) → 旧代码整段 try 吞成静默零注入。新契约:
- `_probe_rw()` False (写不可用) → 记账整段跳过 (零 boost-fail 噪声), 召回是纯读,
  命中照常发射 additionalContext;
- `_probe_rw()` True → 记账语义不变 (只对最终注入条 refresh, 先例 _spy_refresh);
- 探测异常落台账 `rw-probe:` 行, 兼作 fs(沙箱)/sqlite(WAL) 鉴别信号。

风格照 test_recall_first_turns.py (db.init(tmp) 隔离 KG + monkeypatch cli.recall)。
"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "hooks"))

import db
import recall_inject as ri

PROMPT = "专家职位 的结论是什么"


def _payload(transcript_path):
    return json.dumps({"prompt": PROMPT, "session_id": "s1",
                       "cwd": "/tmp/fake-proj",
                       "transcript_path": transcript_path},
                      ensure_ascii=False)


def _mk_lexical_kg(tmp_path) -> str:
    """tmp KG: 实体「专家职位」+ 字面命中 fact (LIF/conf 足够过 0.05 地板)。"""
    db.init(tmp_path / "mem.db")
    import store
    eid = store.put_entity("专家职位", "inferred")
    return store.put_fact(eid, "uses", "专家职位 依赖 sqlite-vec 向量索引",
                          extractor="llm", fact_type="permanent",
                          LIF=0.7, confidence=0.8)


def _run_inject(monkeypatch, tmp_path, transcript_lines=()):
    """count=0 (空 transcript) → 首轮档窗内 → 跑到记账段。返回 stdout 文本。"""
    tpath = tmp_path / "t.jsonl"
    tpath.write_text("\n".join(transcript_lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.StringIO(_payload(str(tpath))))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert ri.main() == 0
    return out.getvalue()


def test_probe_false_skips_accounting_but_still_injects(tmp_path, monkeypatch):
    """核心契约: DB 只读 (_probe_rw=False) → 记账零调用, 注入照常发射。"""
    fid = _mk_lexical_kg(tmp_path)
    monkeypatch.setattr(ri, "_probe_rw", lambda: False)
    calls = []
    import scoring
    monkeypatch.setattr(scoring, "refresh_lif_on_recall",
                        lambda x, **k: calls.append(x))
    monkeypatch.setattr(scoring, "record_recall_observation",
                        lambda *a, **k: calls.append("obs"))
    raw = _run_inject(monkeypatch, tmp_path)
    assert raw, "DB 只读也必须照常注入 (召回是纯读)"
    ctx = json.loads(raw)["hookSpecificOutput"]["additionalContext"]
    assert "sqlite-vec" in ctx
    assert calls == [], f"只读降级: 记账必须整段跳过, 实际 {calls}"
    assert fid  # 命中 sanity


def test_probe_true_keeps_accounting(tmp_path, monkeypatch):
    """探测可写 → 记账语义不变 (refresh 只对最终注入条)。"""
    fid = _mk_lexical_kg(tmp_path)
    monkeypatch.setattr(ri, "_probe_rw", lambda: True)
    calls = []
    import scoring
    monkeypatch.setattr(scoring, "refresh_lif_on_recall",
                        lambda x, **k: calls.append(x))
    raw = _run_inject(monkeypatch, tmp_path)
    assert raw and calls == [fid], f"可写路径记账语义不变, 实际 {calls}"


def test_probe_rw_ok_on_writable_db(tmp_path, monkeypatch):
    """真实探测: 可写 tmp 库 → True (CREATE/DROP _rw_probe 零残留)。"""
    db.init(tmp_path / "mem.db")
    assert ri._probe_rw() is True
    import sqlite3
    conn = db.get_conn()
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                       "AND name='_rw_probe'").fetchone()
    assert row is None, "探测表必须零残留"


def test_probe_rw_logs_fs_and_sqlite_failure(tmp_path, monkeypatch):
    """fs 写 + sqlite 写双失败 → False 且台账落 rw-probe 行 (鉴别信号)。"""
    logged = []
    monkeypatch.setattr(ri, "_log_fail", logged.append)
    # fs 写失败: SVC_DIR/data 指向不可写目录 (monkeypatch SVC_DIR → tmp 空目录下
    # 的只读路径不存在直接建失败不严谨 — 改为指到一个文件路径上必失败)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(ri, "SVC_DIR", blocker)
    import db as db_mod
    monkeypatch.setattr(db_mod, "get_conn",
                        lambda: (_ for _ in ()).throw(
                            __import__("sqlite3").OperationalError(
                                "attempt to write a readonly database")))
    assert ri._probe_rw() is False
    assert len(logged) == 1 and "rw-probe" in logged[0]
    assert "fs=FAIL" in logged[0] and "readonly database" in logged[0]
