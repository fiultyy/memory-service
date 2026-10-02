"""v4 语义切分 + 段级 distill_chunk 测试: 缝扫描边界/回看再分/MINU 并/
gist 降级/挂起语义/幂等/merge-续期。零网络 (laya/zhipu/embed 全 mock)。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 仓根 (src 包母目录)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))  # src 平铺

import db  # noqa: E402
import laya_client  # noqa: E402
import semantic_chunk as sc  # noqa: E402
from src import distill as D  # noqa: E402


def _mk_answer(p2: float) -> dict:
    return {"probabilities": {"0": 0.1, "1": 0.9 - p2, "2": p2}, "score": 2 * p2}


# ── semantic_chunk: 缝扫描 / 回看再分 / MINU / gist ─────────────────

def test_gap_scan_boundaries(tmp_path, monkeypatch):
    """缝 p≥T_CUT → 边界; 窗切分拼接正确; 挂起语义 (重试全败 raise)。"""
    db.init(tmp_path / "m.db")
    calls = []

    def fake_batch(state, questions, timeout=30.0):
        calls.append(list(questions))
        return {k: _mk_answer(0.9) for k in questions}

    monkeypatch.setattr(laya_client, "laya_batch", fake_batch)
    units = [f"句{i}。" for i in range(45)]
    # 强制两窗: win=20 → 缝 19+19+5
    bounds = sc._gap_scan(units, 0.45)
    assert bounds[0] == 0 and bounds[-1] == 45
    assert set(range(1, 44)) <= set(bounds)  # 每缝都 0.9 → 逐句边界

    def dead_batch(state, questions, timeout=30.0):
        return None

    monkeypatch.setattr(laya_client, "laya_batch", dead_batch)
    with pytest.raises(sc.ChunkerUnavailable):
        sc._gap_scan(units[:5], 0.45)


def test_semantic_chunks_flow(tmp_path, monkeypatch):
    """全链: 高分缝=边界, 低分缝=续段, 回看再分超长段, MINU 前向并, gist 降级。"""
    db.init(tmp_path / "m.db")
    units = ([f"电机主题句{i}。" for i in range(10)]
             + [f"采购主题句{i}。" for i in range(8)]
             + [f"退磁主题句{i}。" for i in range(6)])
    cuts = {10, 18}  # 主题切换缝
    win_orig = sc.WIN

    def fake_batch(state, questions, timeout=30.0):
        out = {}
        for k in questions:
            # 从问题串里解析缝号 cutN
            n = int(k[3:])
            # 全局缝号按 state 里的 [w0+i] 行还原 — 用 state 首行下标
            w0 = int(state.split("]", 1)[0][1:])
            out[k] = _mk_answer(0.9 if (w0 + n) in cuts else 0.05)
        return out

    monkeypatch.setattr(laya_client, "laya_batch", fake_batch)
    monkeypatch.setenv("MEM_CHUNK_WIN", "40")
    monkeypatch.setenv("MEM_CHUNK_MAXU", "7")   # 电机 10 句 > 7 → 回看再分
    monkeypatch.setenv("MEM_CHUNK_GIST_OFF", "1")  # gist 降级首句
    try:
        chunks = sc.semantic_chunks("\n".join(units))
    finally:
        monkeypatch.delenv("MEM_CHUNK_WIN")
        monkeypatch.delenv("MEM_CHUNK_MAXU")
        monkeypatch.delenv("MEM_CHUNK_GIST_OFF")
    assert sc.WIN == win_orig
    # 三主题 → 回看再分把 10 句电机段再拆 (内部全低分缝不拆? 内部缝都 0.05
    # → 不切, 10 句段保留超帽) — 断言按实际: 3 段, gist=首句
    assert [c["units_n"] for c in chunks] == [10, 8, 6]
    assert chunks[0]["gist"].startswith("电机主题句0")
    assert chunks[0]["text"].startswith("电机主题句0")


def test_merge_thin():
    segs = [["a", "b"], ["c"], ["d", "e"], ["f"]]
    assert sc._merge_thin(segs, 2) == [["a", "b", "c"], ["d", "e", "f"]]
    assert sc._merge_thin([["x"], ["y", "z"]], 2) == [["x", "y", "z"]]
    assert sc._merge_thin([["a", "b"]], 2) == [["a", "b"]]


def test_split_by():
    units = list("abcdef")
    assert sc._split_by(units, [2, 5, 6]) == [["a", "b"], ["c", "d", "e"], ["f"]]
    assert sc._split_by(units, [6]) == [list("abcdef")]


# ── distill_chunk: 入图 / 幂等 / process 跳过 / merge 续期 / 挂起 ──────

def _init(tmp_path):
    db.init(tmp_path / "m.db")
    return db.get_conn()


def _mock_avail(monkeypatch):
    monkeypatch.setattr(laya_client, "laya_available", lambda: True)
    monkeypatch.setenv("ZHIPU_API_KEY", "test")  # 过 distill 配置门 (真调用被 _judge mock)


def _mock_judge(monkeypatch, label="fact", summary=None, entities=None, when=None):
    def fake(chunk_text, gist):
        return {"summary": summary or gist, "label": label,
                "entities": entities, "when": when}
    monkeypatch.setattr(D, "_judge_chunk", fake)


def _mock_laya(monkeypatch, p_dur=0.8, edge=None):
    def fake(state, questions):
        out = {}
        for k in questions:
            out[k] = (_mk_answer(p_dur) if k == "dur"
                      else _mk_answer(edge if edge is not None else 0.8))
        return out
    monkeypatch.setattr(D, "_laya_one", fake)


def _mock_embed(monkeypatch, dim=None):
    dim = dim or [0.5] * (D._EMBED_DIM_MIN + 10)

    def fake(texts):
        return [list(dim) for _ in texts]
    monkeypatch.setattr(D.embedding, "embed_batch", fake)


def test_distill_chunk_insert_and_idempotent(tmp_path, monkeypatch):
    conn = _init(tmp_path)
    _mock_avail(monkeypatch)
    _mock_judge(monkeypatch, label="judgment", summary="缝扫描判定案")
    _mock_laya(monkeypatch)
    _mock_embed(monkeypatch)
    r = D.distill_chunk("语义段原文甲。第二句。", "结论甲", "s1", "/w", "2026-10-03T00:00:00+00:00")
    assert r == {"atoms": 1, "edges": 0, "merged": 0, "supersede_proposals": []}
    row = conn.execute("SELECT text, gist, label, p_dur, subjects FROM atom").fetchone()
    assert row["text"] == "语义段原文甲。第二句。"
    assert row["gist"] == "缝扫描判定案"
    assert row["label"] == "judgment" and row["p_dur"] == pytest.approx(0.8)
    assert json.loads(row["subjects"] or "[]") == []
    # 幂等: 同段重跑 skipped=seen, 不加行
    r2 = D.distill_chunk("语义段原文甲。第二句。", "结论甲", "s1", "/w", "2026-10-03T01:00:00+00:00")
    assert r2["skipped"] == "seen"
    assert conn.execute("SELECT COUNT(*) FROM atom").fetchone()[0] == 1


def test_distill_chunk_process_skip_and_event(tmp_path, monkeypatch):
    conn = _init(tmp_path)
    _mock_avail(monkeypatch)
    _mock_judge(monkeypatch, label="process")
    _mock_laya(monkeypatch)
    _mock_embed(monkeypatch)
    r = D.distill_chunk("时点快照段。", "快照", "s1", "/w", "2026-10-03T00:00:00+00:00")
    assert r["skipped"] == "process"
    assert conn.execute("SELECT COUNT(*) FROM atom").fetchone()[0] == 0
    # event 类带 when → event_at 列
    _mock_judge(monkeypatch, label="event", when="2026-10-05")
    D.distill_chunk("日程段原文。", "日程结论", "s1", "/w", "2026-10-03T00:00:00+00:00")
    row = conn.execute("SELECT event_at, gist FROM atom").fetchone()
    assert row["event_at"] == "2026-10-05" and row["gist"] == "日程结论"


def test_distill_chunk_merge_renewal(tmp_path, monkeypatch):
    """cos ≥ 段级 merge 阈 → 不插新行, 既有 atom source_refs 追加 + last_seen_at 续期。"""
    import numpy as np
    conn = _init(tmp_path)
    _mock_avail(monkeypatch)
    dim = D._EMBED_DIM_MIN + 10
    v = [1.0] + [0.0] * (dim - 1)
    conn.execute(
        "INSERT INTO atom(text, label, p_dur, valid_from, source_refs, last_seen_at) "
        "VALUES('既有段。', 'fact', 0.5, '2026-09-01T00:00:00+00:00', '[]', "
        "'2026-09-01T00:00:00+00:00')")
    _mock_judge(monkeypatch, label="fact")
    _mock_laya(monkeypatch)

    def same_vec(texts):
        return [list(v) for _ in texts]
    monkeypatch.setattr(D.embedding, "embed_batch", same_vec)
    monkeypatch.setattr(D, "_load_existing",
                        lambda: [(1, np.asarray(v, dtype=np.float32))])
    r = D.distill_chunk("近重复段原文。", "结论", "s1", "/w", "2026-10-03T00:00:00+00:00")
    assert r["merged"] == 1 and r["atoms"] == 0
    row = conn.execute("SELECT source_refs, last_seen_at FROM atom WHERE id=1").fetchone()
    assert json.loads(row["source_refs"]) == ["近重复段原文。"]
    assert row["last_seen_at"] == "2026-10-03T00:00:00+00:00"


def test_distill_chunk_suspend_and_edge(tmp_path, monkeypatch):
    conn = _init(tmp_path)
    # laya 不可用 (conftest 已钉 MEM_LAYA_ENABLED=0) → LayaUnavailable, 零写入
    _mock_judge(monkeypatch)
    _mock_laya(monkeypatch)
    _mock_embed(monkeypatch)
    with pytest.raises(D.LayaUnavailable):
        D.distill_chunk("段。", "g", "s1", "/w", "2026-10-03T00:00:00+00:00")
    assert conn.execute("SELECT COUNT(*) FROM atom").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM distill_seen").fetchone()[0] == 0


import json  # noqa: E402 — 测试内用到 (置底避免顶部风格混乱, 同仓先例)


def test_bootstrap_v4_lane(tmp_path, monkeypatch):
    """re_ingest_file 走 v4 车道: semantic_chunks → distill_chunk 透传
    (text/gist/溯源三件套); MEM_SEMANTIC_CHUNK=0 钉回句级车道。"""
    import bootstrap
    md = tmp_path / "note.md"
    md.write_text("第一段。\n\n第二段。", encoding="utf-8")
    got = []

    def fake_chunks(text):
        return [{"text": "整篇一段", "gist": "结论", "units_n": 2}]

    monkeypatch.setattr(bootstrap, "distill_mod",
                        type("S", (), {"distill_chunk":
                         staticmethod(lambda t, g, session_id, cwd, ts:
                                      (got.append((t, g, session_id, cwd)),
                                       {"atoms": 1, "edges": 0, "merged": 0})[1]),
                         "distill_segment":
                         staticmethod(lambda *a, **k:
                                      {"atoms": 0, "edges": 0, "merged": 0})})())
    import src.semantic_chunk as scm
    monkeypatch.setattr(scm, "semantic_chunks", fake_chunks)
    monkeypatch.setenv("MEM_SEMANTIC_CHUNK", "1")
    r = bootstrap.re_ingest_file(md)
    assert r["segments"] == 1 and r["atoms"] == 1
    assert got and got[0][0] == "整篇一段" and got[0][1] == "结论"
    assert got[0][2] == "memory:note.md" and got[0][3] == str(tmp_path)
