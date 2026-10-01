"""fallback lane + ⑤a 注入降级标注验收测试 (graph-reform v2 H3a 重写, 裁决#4/#5b).

2026-10-01 语义反转: fallback:auto 的 gazetteer 降级链**全面退役** —
provider 不可达 = 挂起等恢复 (ProviderUnreachable 响亮上抛), 不再产离线
记忆。本文件重写钉新行为; 既有读侧面 (⑤a 降级标注渲染 / E9/C2 三口分账 /
打标面=清洗面闭环) 不受写侧退役影响, 照旧覆盖:

1. 裁决#5b: fallback:auto 档 llm 断供 → ProviderUnreachable 上抛挂起,
   零 fact / 零 upgrade_queue (不降级不产离线记忆); 默认 llm 档同红线。
2. 单段内容性 ExtractFailed → 响亮跳段继续 (B4-DISTILL, 爆炸半径=1 段)。
3. E9/C2 三口分账: 降级来源 (存量 extractor='regex') fact 被召回/注入/重放
   → LIF 列零变化, 只写 recall_sessions 观测集 (⑤a 警示渲染同测)。
4. E12: embed 预热炸点被 try/except 吸收, 管道照常落库。
5. 闭环: ⑤a 降级标注块被 corpus_prep 五 harness 整块剥净 (打标面=清洗面)。

测试规范: def test_xxx() 函数让 pytest 收集。零网络零 LLM: llm_extract.extract
monkeypatch 注入 ProviderUnreachable / 可控 Extraction; embedding.embed stub
离线; signals 目录指向 tmp。
"""
import io
import json
import sys
import tempfile
from pathlib import Path

import db
import dream
import embedding
import llm_extract
import scoring
import signals
import store
from llm_extract import ExtractFailed, ProviderUnreachable
from llm_provider import EdgeOut, EntityOut, Extraction

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "hooks"))

import recall_inject as ri  # noqa: E402  (hooks 路径插入后)


# ── 夹具 ─────────────────────────────────────────────────────────────

_SENT = "Logseq 是笔记工具"


def _fresh(name: str) -> Path:
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / f"{name}.db")
    return Path(tmp)


def _patch_signals(sig_dir: Path):
    orig = signals._signals_dir
    signals._signals_dir = lambda: sig_dir
    return lambda: setattr(signals, "_signals_dir", orig)


def _write_transcript(path: Path, text: str = _SENT) -> str:
    path.write_text(json.dumps(
        {"type": "user", "message": {"content": [{"type": "text", "text": text}]}},
        ensure_ascii=False) + "\n", encoding="utf-8")
    return str(path)


def _offline_embed(monkeypatch):
    monkeypatch.setattr(embedding, "embed", lambda text, providers=None: [])


def _llm_down(monkeypatch):
    def _boom(text, provider=None):
        raise ProviderUnreachable("simulated provider outage")
    monkeypatch.setattr(llm_extract, "extract", _boom)


def _llm_extraction(subject: str, predicate: str, obj: str) -> Extraction:
    return Extraction(
        entities=[EntityOut(name=subject, type="concept"),
                  EntityOut(name=obj, type="concept")],
        edges=[EdgeOut(subject=subject, predicate=predicate, object=obj)],
        confidence=0.9,
        source_meta={"provider": "mock", "extractor_label": "llm"},
    )


def _mock_llm_extract(monkeypatch, subject: str, predicate: str, obj: str):
    monkeypatch.setattr(
        llm_extract, "extract",
        lambda text, provider=None: _llm_extraction(subject, predicate, obj))


def _active_fact(value: str) -> dict:
    rows = db.get_conn().execute(
        "SELECT id FROM fact WHERE value = ? AND status = 'active'",
        (value,)).fetchall()
    assert len(rows) == 1, f"fixture: value={value!r} 应恰 1 条 active"
    return store.get_fact(rows[0]["id"])


# ── 1. 裁决#5b: 不可达 = 挂起 (降级链退役) ───────────────────────────

def test_fallback_auto_unreachable_suspends(monkeypatch):
    """fallback:auto + llm 断供 → ProviderUnreachable 响亮上抛挂起等恢复:
    零 fact 落库、零 upgrade_queue 入队 — 不再切 gazetteer 产离线记忆。"""
    tmp = _fresh("fb1")
    tpath = _write_transcript(tmp / "t.jsonl")
    _offline_embed(monkeypatch)
    _llm_down(monkeypatch)
    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", "fallback:auto")

    import autodream
    try:
        autodream.autodream("s1", tpath)
    except ProviderUnreachable:
        pass  # 挂起 = 预期 (响亮上抛)
    else:
        raise AssertionError("fallback:auto 断供必须挂起上抛 (降级链已退役)")
    n_fact = db.get_conn().execute("SELECT COUNT(*) FROM fact").fetchone()[0]
    assert n_fact == 0, f"挂起语义: 不产离线记忆, got {n_fact} facts"
    n_q = db.get_conn().execute(
        "SELECT COUNT(*) FROM upgrade_queue").fetchone()[0]
    assert n_q == 0, f"降级入队链已退役, got {n_q} queue rows"


def test_default_llm_channel_stays_loud(monkeypatch):
    """断供红线: 默认 llm 档断供 (ProviderUnreachable) → 响亮上抛, 不静默降级。"""
    tmp = _fresh("fb2")
    tpath = _write_transcript(tmp / "t.jsonl")
    _offline_embed(monkeypatch)
    _llm_down(monkeypatch)
    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", "llm")
    import autodream
    try:
        autodream.autodream("s1", tpath)
    except ExtractFailed:
        pass  # 响亮 = 预期
    else:
        raise AssertionError("默认 llm 档断供必须响亮上抛 (无降级红线)")


def test_schema_fail_skips_segment_continues(monkeypatch):
    """B4-DISTILL (2026-09-01): 默认 llm 档**单段内容性** schema 两轮败 →
    响亮跳段, 其余段产出照常落库 (爆炸半径=1 段)。断供仍走 stays_loud 红线。"""
    tmp = _fresh("fb2b")
    # 两块异 provenance (user_text + assistant_text) → 两段
    tpath = str(tmp / "t2.jsonl")
    with open(tpath, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "message": {"content": [
            {"type": "text", "text": _SENT}]}}, ensure_ascii=False) + "\n")
        fh.write(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "React 是一个组件化前端框架"}]}},
            ensure_ascii=False) + "\n")
    _offline_embed(monkeypatch)

    def _seg_extract(text, provider=None):
        if "组件化" in text:  # 该段内容性失败 (模拟 object 未声明两轮败)
            raise ExtractFailed(
                "schema 校验两轮失败 (prompt v6): object 未声明: 'X'")
        return _llm_extraction("Logseq", "is_a", "笔记工具")

    monkeypatch.setattr(llm_extract, "extract", _seg_extract)
    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", "llm")
    import autodream
    r = autodream.autodream("s1", tpath)  # 不上抛 = 跳段继续
    assert r["added"] >= 1, f"好段产出必须落库: {r}"
    assert db.get_conn().execute(
        "SELECT COUNT(*) FROM fact WHERE extractor='llm'").fetchone()[0] >= 1


# ── 3. E9/C2 三口分账 (读侧面, 存量降级来源) ─────────────────────────

def _seed_fallback_fact(tmp: Path) -> tuple[str, str]:
    eid = store.put_entity("Logseq", "concept")
    fid = store.put_fact(eid, "is_a", "笔记工具", extractor="regex",
                         lif_source=0.4)
    return eid, fid


def _assert_fallback_untouched(fid: str, session: str):
    fact = store.get_fact(fid)
    assert fact["access_count"] == 0, (
        f"E9: fallback fact 不得刷 access_count, got {fact['access_count']}")
    assert fact["last_accessed_at"] is None, "E9: 不得刷 last_accessed_at"
    assert session in (fact.get("recall_sessions") or []), (
        "E9: 只写 recall_sessions 观测集 (记忆被使用过的证明)")
    assert fact.get("seen_sessions") in (None, []), (
        "E9/C2: fallback 受限面不得写 seen_sessions")


def test_three_port_fallback_accounting(monkeypatch):
    """直写 / replay / 注入 三口: fallback fact 的 LIF 强化全受限, 仅
    recall_sessions 观测集吸收。"""
    monkeypatch.delenv(scoring.COLDSTART_UNLOCK_ENV, raising=False)

    # ① 直写口: scoring.refresh_lif_on_recall。
    tmp = _fresh("fb3a")
    _, fid = _seed_fallback_fact(tmp)
    scoring.refresh_lif_on_recall(fid, session_id="sx")
    _assert_fallback_untouched(fid, "sx")

    # ② replay 口: dream._replay_recall_hits 消费 recall_hits 流。
    tmp = _fresh("fb3b")
    restore_sig = _patch_signals(tmp / "signals")
    _, fid = _seed_fallback_fact(tmp)
    try:
        signals.append("recall_hits", {"fact_id": fid, "session_id": "sy",
                                       "query": "q", "score": 0.8})
        stats = dream._replay_recall_hits(None)
        assert stats["lif_facts"] == 1, stats
        _assert_fallback_untouched(fid, "sy")
    finally:
        restore_sig()

    # ③ 注入口: recall_inject 注入即使用 — 记账 + 受限跳过 + ⑤a 警示。
    tmp = _fresh("fb3c")
    eid, fid = _seed_fallback_fact(tmp)
    monkeypatch.setenv("MEM_RECALL_MIN_SCORE", "0.05")
    monkeypatch.setenv("MEM_RECALL_MAX_BYTES", "4096")
    import cli
    import recall as recall_mod
    monkeypatch.setattr(recall_mod, "search_entities",
                        lambda toks: [{"id": eid, "name": "Logseq"}])
    hit = {"score": 0.42, "tag": {"display": "Logseq"},
           "fact": {"id": fid, "subject_id": eid, "object_id": None,
                    "value": "笔记工具", "extractor": "regex",
                    "lif_source": 0.4}}
    monkeypatch.setattr(cli, "recall", lambda *a, **k: {"results": [hit]})
    # A1-RW-001-F1: 日志路径隔离 — 零真台账污染 (A1 误诊教训)。
    monkeypatch.setattr(ri, "_log_fail", lambda m: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"prompt": "Logseq 的结论是什么", "session_id": "sz",
         "cwd": "/tmp/fake-proj"})))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert ri.main() == 0
    ctx = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith('<memsvc-recall quality="fallback">'), (
        f"⑤a: 全降级块打 quality 属性, got {ctx[:60]}")
    assert "[warning] 以下各条均产生于降级通道、未经主径 LLM 验证" in ctx
    _assert_fallback_untouched(fid, "sz")


def test_mixed_block_plain_tag_per_entry_warning(monkeypatch):
    """⑤a 混合块: 主径 + 降级共存 → 平标签 + 仅 fallback 条目前插警示行;
    主径条照常 LIF 强化 (对照面)。"""
    tmp = _fresh("fb4")
    monkeypatch.delenv(scoring.COLDSTART_UNLOCK_ENV, raising=False)
    eid = store.put_entity("Logseq", "concept")
    fid_llm = store.put_fact(eid, "is_a", "双链笔记", extractor="llm",
                             lif_source=0.7)
    fid_fb = store.put_fact(eid, "uses", "插件", extractor="regex",
                            lif_source=0.4)
    monkeypatch.setenv("MEM_RECALL_MIN_SCORE", "0.05")
    monkeypatch.setenv("MEM_RECALL_MAX_BYTES", "4096")
    import cli
    import recall as recall_mod
    monkeypatch.setattr(recall_mod, "search_entities",
                        lambda toks: [{"id": eid, "name": "Logseq"}])
    hits = [
        {"score": 0.9, "tag": {"display": "Logseq"},
         "fact": {"id": fid_llm, "subject_id": eid, "object_id": None,
                  "value": "双链笔记", "extractor": "llm",
                  "lif_source": 0.7}},
        {"score": 0.42, "tag": {"display": "Logseq"},
         "fact": {"id": fid_fb, "subject_id": eid, "object_id": None,
                  "value": "插件", "extractor": "regex", "lif_source": 0.4}},
    ]
    monkeypatch.setattr(cli, "recall", lambda *a, **k: {"results": hits})
    # A1-RW-001-F1: 日志路径隔离 — 零真台账污染 (A1 误诊教训)。
    monkeypatch.setattr(ri, "_log_fail", lambda m: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"prompt": "Logseq 的结论是什么", "session_id": "sm",
         "cwd": "/tmp/fake-proj"})))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert ri.main() == 0
    ctx = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("<memsvc-recall>\n"), "混合块平标签"
    assert 'quality="fallback"' not in ctx
    assert ctx.count("[warning] 本条产生于降级通道") == 1, (
        f"仅 fallback 条前一条警示行:\n{ctx}")
    assert ctx.index("[warning]") > ctx.index("双链笔记"), (
        "警示行位于 fallback 条目前 (主径条之后)")
    # 对照面: 主径条非受限 → 照常 LIF 强化; fallback 条零变化。
    f_llm = store.get_fact(fid_llm)
    assert f_llm["access_count"] == 1, "主径条注入照常强化"
    assert "sm" in (f_llm.get("seen_sessions") or [])
    _assert_fallback_untouched(fid_fb, "sm")


# ── 5. ⑤a 闭环: 打标面 = 清洗面 ─────────────────────────────────────

def test_fallback_warning_block_not_reingested(monkeypatch):
    """⑤a 降级标注块 (quality 属性 + 警示行) 被 corpus_prep 五 harness 整块
    剥净 — 语料重入库零回流 (闭环不变式在带属性形态下仍成立)。"""
    from corpus_prep import HARNESSES, clean
    all_fb = (
        '<memsvc-recall quality="fallback">\n'
        "## Memory recall (auto, 1 hits)\n"
        "[warning] 以下各条均产生于降级通道、未经主径 LLM 验证，需自行判断召回准确性\n"
        "- Logseq — 笔记工具  [0.42]\n"
        "</memsvc-recall>"
    )
    mixed = (
        "<memsvc-recall>\n"
        "## Memory recall (auto, 2 hits)\n"
        "- Logseq — 双链笔记  [0.90]\n"
        "- [warning] 本条产生于降级通道、未经主径 LLM 验证，需自行判断召回准确性\n"
        "- Logseq — 插件  [0.42]\n"
        "</memsvc-recall>"
    )
    for h in HARNESSES:
        assert clean(all_fb, h) == "", f"全降级块回流: {h}"
        assert clean(mixed, h) == "", f"混合块回流: {h}"


# ── 6. E12 embedding 预热炸点降级 (管道不炸) ─────────────────────────

def test_embed_down_pipeline_still_completes(monkeypatch):
    """E12 N6: autodream 的 embed_batch 预热炸点 (fact value 预热) raise 被
    try/except 吸收 (写侧 vec 条件跳过), llm 主径产出照常落库 — 单事务不炸。"""
    tmp = _fresh("fb6")
    tpath = _write_transcript(tmp / "t.jsonl")
    _mock_llm_extract(monkeypatch, "Logseq", "is_a", "笔记工具")
    _offline_embed(monkeypatch)
    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", "llm")
    real_batch = embedding.embed_batch

    def _raise_on_value_warmup(texts, providers=None):
        if [t for t in texts] == ["笔记工具"]:
            raise RuntimeError("emb batch down")  # E12 炸点: fact value 预热
        return real_batch(texts, providers=providers)

    monkeypatch.setattr(embedding, "embed_batch", _raise_on_value_warmup)
    import autodream
    out = autodream.autodream("s1", tpath)  # 不 raise = 验收
    assert out["added"] == 1, out
    assert _active_fact("笔记工具")["extractor"] == "llm"
