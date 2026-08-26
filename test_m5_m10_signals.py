"""M5 信号流 + M10 强化改道批验收测试 (spec v2 §1 M5 + §3 M10/M10-v2, DR-7 G6/G7)。

覆盖派发令四条验收:
1. M5 API: append 自动 ts/惰性建目录/五流不混写/未知流拒绝/损坏行读侧容错。
2. M10 env=0: 既有 recall 行为零变化 (access_count 写回发生)。
3. M10 env=1: recall 零写回 (access_count/last_accessed_at 不变) + recall_hits
   流逐命中一条、字段全 (fact_id/session_id/query/score/source_cwd)。
4. 往返对账: env=1 命中数 == 信号记录数; env 切回 0 恢复写回 (灰度可回退)。

测试规范: def test_xxx() 函数让 pytest 收集。禁网络/LLM: 直接 store 插 fact
绕过提取; signals 目录 monkeypatch 指向 tmp (不污染 data/signals)。
"""
import os
import tempfile
from pathlib import Path

import db
import recall as recall_mod
import signals
import store


def _fresh(tmpdir: str | None = None) -> tuple[str, Path]:
    """tmp db + tmp signals dir (隔离), 返回 (entity_id, signals_tmp)。"""
    tmp = tmpdir or tempfile.mkdtemp()
    db.init(Path(tmp) / "m5.db")
    sig_dir = Path(tmp) / "signals"
    return tmp, sig_dir


def _patch_signals_dir(sig_dir: Path):
    """把 signals 目录指到 tmp; 返回还原函数。"""
    orig = signals._signals_dir
    signals._signals_dir = lambda: sig_dir
    return lambda: setattr(signals, "_signals_dir", orig)


def _seed_fact(value: str = "rust", source_cwd: str = "/test") -> str:
    """造一条可被 recall('rust') 命中的 fact, 返回 fact_id。"""
    eid = store.put_entity("用户", "inferred")
    return store.put_fact(
        eid, "uses", value, extractor="llm", fact_type="permanent",
        source_cwd=source_cwd, LIF=0.6, confidence=0.8,
        source_refs=["session:s"], topic="用户使用 rust 开发")


def _recall_hits_rows() -> list:
    return signals.read("recall_hits")


# ── 验收 1: M5 API ───────────────────────────────────────────────────

def test_append_auto_ts_lazy_dir():
    tmp, sig = _fresh()
    restore = _patch_signals_dir(sig)
    try:
        assert not sig.exists(), "目录应惰性创建 (append 前不建)"
        signals.append("recall_hits", {"fact_id": "f1", "session_id": "s1",
                                       "query": "q", "score": 0.8,
                                       "source_cwd": "/proj"})
        assert sig.is_dir() and (sig / "recall_hits.jsonl").is_file()
        rows = signals.read("recall_hits")
        assert len(rows) == 1
        row = rows[0]
        assert row["ts"] and row["ts"].endswith("+00:00"), (
            f"ts 应自动补秒级 UTC ISO, got {row.get('ts')}")
        assert row["fact_id"] == "f1" and row["source_cwd"] == "/proj"
        # append-only: 二次追加 → 两行, 首行不改写。
        signals.append("recall_hits", {"fact_id": "f2"})
        assert len(signals.read("recall_hits")) == 2
        assert signals.read("recall_hits")[0]["fact_id"] == "f1"
    finally:
        restore()


def test_five_streams_no_cross_write():
    tmp, sig = _fresh()
    restore = _patch_signals_dir(sig)
    try:
        payloads = {
            "recall_hits": {"fact_id": "f"},
            "agent_crud": {"verb": "write", "fact_id": "f"},
            "citations": {"fact_id": "f", "agent_output_ref": "ref"},
            "confirm_arrivals": {"fact_id": "f", "via": "cli"},
            "human_proj_ops": {"op": "edit", "path": "/p", "detail": "d"},
        }
        for stream, rec in payloads.items():
            signals.append(stream, rec)
        for stream, rec in payloads.items():
            rows = signals.read(stream)
            assert len(rows) == 1, f"{stream} 应恰 1 行, got {len(rows)}"
            key = next(iter(rec))
            assert rows[0][key] == rec[key]
        # 默认 source_cwd=None 公共字段补齐。
        assert all(r["source_cwd"] is None and r["ts"]
                   for s in payloads for r in signals.read(s))
    finally:
        restore()


def test_unknown_stream_rejected():
    tmp, sig = _fresh()
    restore = _patch_signals_dir(sig)
    try:
        try:
            signals.append("typo_stream", {"x": 1})
        except ValueError:
            pass
        else:
            raise AssertionError("未知流必须 ValueError (防混写)")
        assert not (sig / "typo_stream.jsonl").exists()
    finally:
        restore()


def test_read_tolerates_corrupt_lines():
    tmp, sig = _fresh()
    restore = _patch_signals_dir(sig)
    try:
        signals.append("citations", {"fact_id": "ok1"})
        # 注入损坏行 (半行写入 / 非 JSON)。
        with (sig / "citations.jsonl").open("a", encoding="utf-8") as fh:
            fh.write('{"fact_id": "half"\n')
            fh.write("not json at all\n")
        signals.append("citations", {"fact_id": "ok2"})
        rows = signals.read("citations")
        assert [r["fact_id"] for r in rows] == ["ok1", "ok2"], (
            f"损坏行应被跳过, got {rows}")
    finally:
        restore()


def test_read_missing_stream_empty():
    tmp, sig = _fresh()
    restore = _patch_signals_dir(sig)
    try:
        assert signals.read("human_proj_ops") == []
    finally:
        restore()


# ── 验收 2: M10 env=0 — 既有行为零变化 ───────────────────────────────

def _env_off():
    os.environ.pop("MEM_DELAYED_REINFORCE", None)


def test_env_absent_writeback_happens():
    """缺省 (env 未设): ADR-8v2 即时写回发生 — access_count 1, last_accessed_at
    落值, seen_sessions 吸收 session; 信号流零记录 (旧行为路径不变)。"""
    tmp, sig = _fresh()
    fid = _seed_fact()
    restore = _patch_signals_dir(sig)
    _env_off()
    try:
        recall_mod.recall("rust", session_id="s1")
    finally:
        restore()
    f = store.get_fact(fid)
    assert f["access_count"] == 1, f"写回应发生, got access_count={f['access_count']}"
    assert f["last_accessed_at"] is not None
    assert "s1" in f["seen_sessions"]
    assert _recall_hits_rows_isolated(sig) == [], "env 缺省不得写信号流"


def test_env_zero_writeback_happens():
    """显式 env=0: 同缺省 — 写回发生零信号。"""
    tmp, sig = _fresh()
    fid = _seed_fact()
    restore = _patch_signals_dir(sig)
    os.environ["MEM_DELAYED_REINFORCE"] = "0"
    try:
        recall_mod.recall("rust", session_id="s1")
    finally:
        os.environ.pop("MEM_DELAYED_REINFORCE", None)
        restore()
    assert store.get_fact(fid)["access_count"] == 1


def _recall_hits_rows_isolated(sig: Path) -> list:
    orig = signals._signals_dir
    signals._signals_dir = lambda: sig
    try:
        return signals.read("recall_hits")
    finally:
        signals._signals_dir = orig


# ── 验收 3: M10 env=1 — 零写回 + 信号流逐命中一条 ────────────────────

def test_env_on_no_writeback_signal_written():
    tmp, sig = _fresh()
    fid = _seed_fact(source_cwd="/proj-x")  # 与查询 cwd 对齐 (ADR-14 过滤)
    restore = _patch_signals_dir(sig)
    os.environ["MEM_DELAYED_REINFORCE"] = "1"
    mem_dir = Path(tmp) / "memory"
    mem_dir.mkdir()
    try:
        before = store.get_fact(fid)
        res = recall_mod.recall("rust", session_id="s9", cwd="/proj-x",
                                mem_dir=str(mem_dir))
    finally:
        os.environ.pop("MEM_DELAYED_REINFORCE", None)
        restore()

    assert res, "recall 结果不应受 env 影响 (纯读仍返回命中)"
    after = store.get_fact(fid)
    assert after["access_count"] == before["access_count"] == 0, (
        f"env=1 零写回: access_count 应不变, got {after['access_count']}")
    assert after["last_accessed_at"] == before["last_accessed_at"]
    assert after["seen_sessions"] == before["seen_sessions"]
    rows = _recall_hits_rows_isolated(sig)
    assert len(rows) == 1, f"逐命中一条, got {len(rows)}"
    row = rows[0]
    assert row["fact_id"] == fid
    assert row["session_id"] == "s9"
    assert row["query"] == "rust"
    assert isinstance(row["score"], float) and row["score"] >= 0.3
    assert row["source_cwd"] == "/proj-x", "source_cwd 字段隔离必须落 (DR-7 G6)"
    assert row["ts"]


def test_env_on_boost_false_still_pure():
    """显式 boost=False + env=1: 调用方已自弃强化 → 纯读零信号 (无可改道事件)。"""
    tmp, sig = _fresh()
    fid = _seed_fact()
    restore = _patch_signals_dir(sig)
    os.environ["MEM_DELAYED_REINFORCE"] = "1"
    try:
        recall_mod.recall("rust", session_id="s1", boost=False)
    finally:
        os.environ.pop("MEM_DELAYED_REINFORCE", None)
        restore()
    assert store.get_fact(fid)["access_count"] == 0
    assert _recall_hits_rows_isolated(sig) == []


# ── 验收 4: 往返对账 + 灰度可回退 ────────────────────────────────────

def test_roundtrip_and_revert():
    """env=1: 信号记录数 == 命中数 (top_k=2 两命中); env 切回 0: 写回恢复
    (access_count 增长) 且信号流零新增 (灰度可回退性)。"""
    tmp, sig = _fresh()
    e1 = store.put_entity("用户", "inferred")
    f1 = store.put_fact(e1, "uses", "rust", extractor="llm", fact_type="permanent",
                        source_cwd="/test", LIF=0.6, confidence=0.8, topic="t1")
    e2 = store.put_entity("项目", "inferred")
    f2 = store.put_fact(e2, "uses", "rust", extractor="llm", fact_type="permanent",
                        source_cwd="/test", LIF=0.6, confidence=0.8, topic="t2")

    restore = _patch_signals_dir(sig)
    os.environ["MEM_DELAYED_REINFORCE"] = "1"
    try:
        res = recall_mod.recall("rust", session_id="s1", top_k=2)
    finally:
        os.environ.pop("MEM_DELAYED_REINFORCE", None)
    hits = {f["id"] for f in res}
    rows = _recall_hits_rows_isolated(sig)
    assert len(res) == 2 and len(rows) == 2, (
        f"对账: 命中 {len(res)} == 信号 {len(rows)}")
    assert {r["fact_id"] for r in rows} == hits
    assert all(store.get_fact(fid)["access_count"] == 0 for fid in (f1, f2))

    # 回退: env 切回 0 → 写回恢复 + 信号零新增。
    os.environ.pop("MEM_DELAYED_REINFORCE", None)
    try:
        recall_mod.recall("rust", session_id="s1", top_k=2)
    finally:
        restore()
    assert all(store.get_fact(fid)["access_count"] == 1
               for fid in (f1, f2)), "env 切回 0 写回必须恢复"
    assert len(_recall_hits_rows_isolated(sig)) == 2, "回退后信号流零新增"
