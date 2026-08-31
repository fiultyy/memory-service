"""v1.7④ 冷启动 D6 双窗口 + E3/E4/E5/E6/E7 分账验收测试 (派单 E3-E12 组4)。

覆盖:
1. E5 解锁判据纯函数: len(extract_sessions)>=2 双分支 (1 不解锁 / 2 解锁)。
2. E6/C1a: regex 通道复现同 (s,p,v) 的 UPDATE 不把 session stamp 进
   extract_sessions (单通道凑不满解锁); seen_sessions 照旧吸收。
3. E3 bootstrap: session_id=memory:<file>#<ci> 两列 stamp 统一虚拟会话
   "self" (len 天然封顶 1, 不可凑), session_id 本体只进 source_refs。
4. M4 反向修正: 非 fallback replay 只写 seen_sessions, 不碰
   extract_sessions (replay 是使用痕迹, 不是提取证据)。
5. D6 暂缓期冻结: 判据直驱 + env 默认关 (只写不读, 刷新面跑旧规则);
   fallback 来源受限不受门控制 (无条件)。
6. E4 dedup merge: seen/extract 并集幂等 + survivor extractor 取 group
   最高档 (human>vote>llm>regex, 严格大于才换)。
7. 低初值: 主径 llm brand-new ADD lif_source=0.4 (待验证) + 解锁期毕业
   (len=2 UPDATE → lif_source 毕业到 extractor 真值档 0.7)。
8. E7 C1b 矩阵: regex 挑战 llm → NOOP + contradiction_pending 七字段 +
   segcontra: 复活入队 (done 重入返 id 不返 None) + 消费端 rerun 仲裁;
   llm 挑战 regex → supersede 照旧 (无信号无入队)。

测试规范: def test_xxx() 函数让 pytest 收集。零网络零 LLM: llm 主径经
monkeypatch llm_extract.extract 注入可控 Extraction; regex 通道走词典链;
embedding.embed stub 离线; signals 目录指向 tmp。
"""
import json
import tempfile
from pathlib import Path

import autodream
import consolidate
import db
import dream
import embedding
import llm_extract
import scoring
import signals
import store
import upgrade
from llm_extract import ExtractFailed
from llm_provider import EdgeOut, EntityOut, Extraction


# ── 夹具 ─────────────────────────────────────────────────────────────

def _fresh(name: str) -> Path:
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / f"{name}.db")
    return Path(tmp)


def _patch_signals(sig_dir: Path):
    orig = signals._signals_dir
    signals._signals_dir = lambda: sig_dir
    return lambda: setattr(signals, "_signals_dir", orig)


def _write_transcript(path: Path, text: str) -> str:
    path.write_text(json.dumps(
        {"type": "user", "message": {"content": [{"type": "text", "text": text}]}},
        ensure_ascii=False) + "\n", encoding="utf-8")
    return str(path)


class _FakeJudge:
    """contradiction judge mock: 恒判 True (单值谓词矛盾成立)。"""

    def judge_contradiction(self, subject_type, subject_name, predicate,
                            new_value, old_value):
        return {"contradiction": True, "reason": "fake"}


def _llm_extraction(subject: str, predicate: str, obj: str) -> Extraction:
    """主径 llm 档 Extraction (extractor_label='llm' — C1a 计数开关)。"""
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


def _fact_by_value(subject: str, value: str) -> dict:
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id FROM fact WHERE value = ? AND status = 'active'",
        (value,)).fetchall()
    assert len(rows) == 1, f"fixture: (value={value!r}) 应恰有 1 条 active fact"
    return store.get_fact(rows[0]["id"])


# ── 1. E5 解锁判据纯函数 ─────────────────────────────────────────────

def test_unlock_criterion_len2_dual_branch():
    """len(extract_sessions)>=2 双分支: 0/1 条不解锁, 2 条解锁; 常量=2。"""
    assert scoring.UNLOCK_EXTRACT_SESSIONS == 2
    assert scoring.fact_unlocked({"extract_sessions": []}) is False
    assert scoring.fact_unlocked({"extract_sessions": ["s1"]}) is False
    assert scoring.fact_unlocked({"extract_sessions": ["s1", "s2"]}) is True
    # 坏 JSON/缺列安全读 → 不解锁 (防御面)。
    assert scoring.fact_unlocked({"extract_sessions": "not-json"}) is False
    assert scoring.fact_unlocked({}) is False


# ── 2. E6/C1a regex 通道复现不计数 ───────────────────────────────────

def test_c1a_regex_update_does_not_stamp_extract_sessions(monkeypatch):
    """主径 ADD stamp s1 后, regex 通道 (session s2) 复现同三元组 → UPDATE
    只吸收 seen_sessions, extract_sessions 仍 ['s1'] (C1a 单通道凑不满)。"""
    tmp = _fresh("c1a")
    tpath = _write_transcript(tmp / "t.jsonl", "Logseq 是笔记工具")
    monkeypatch.setattr(embedding, "embed", lambda text, providers=None: [])

    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", "llm")
    _mock_llm_extract(monkeypatch, "Logseq", "is_a", "笔记工具")
    out1 = autodream.autodream("s1", tpath)
    assert out1["added"] == 1, out1
    fact = _fact_by_value("Logseq", "笔记工具")
    assert fact["extractor"] == "llm"
    assert fact["extract_sessions"] == ["s1"]

    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", "regex")  # 词典链复现同三元组
    out2 = autodream.autodream("s2", tpath)
    assert out2["updated"] == 1, out2
    fact = _fact_by_value("Logseq", "笔记工具")
    assert fact["extract_sessions"] == ["s1"], (
        "C1a: regex 通道复现 UPDATE 不得 stamp extract_sessions")
    assert sorted(fact["seen_sessions"]) == ["s1", "s2"], (
        "seen_sessions 照旧吸收 (三口不动)")


# ── 3. E3 bootstrap "self" 封顶 ──────────────────────────────────────

def test_bootstrap_self_stamp_caps_len1(monkeypatch):
    """bootstrap session_id=memory:<file>#<ci> → 两列 stamp 'self' (len=1 封
    顶不可凑), session_id 本体只进 source_refs 溯源。"""
    tmp = _fresh("boot")
    tpath = _write_transcript(tmp / "mb.jsonl", "Logseq 是笔记工具")
    monkeypatch.setattr(embedding, "embed", lambda text, providers=None: [])
    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", "llm")
    _mock_llm_extract(monkeypatch, "Logseq", "is_a", "笔记工具")

    out = autodream.autodream("memory:mb.jsonl#0", tpath)
    assert out["added"] == 1, out
    fact = _fact_by_value("Logseq", "笔记工具")
    assert fact["extract_sessions"] == ["self"], "bootstrap stamp = 虚拟会话 self"
    assert fact["seen_sessions"] == ["self"]
    assert "session:memory:mb.jsonl#0" in (fact["source_refs"] or []), (
        "session_id 本体进 source_refs 溯源不断链")
    assert scoring.fact_unlocked(fact) is False, "len=1 封顶 — bootstrap 不可自解锁"

    # 第二个 bootstrap 批 (#1): stamp 仍 'self' 去重 → len 恒 1。
    out2 = autodream.autodream("memory:mb.jsonl#1", tpath)
    assert out2["updated"] == 1, out2
    fact = _fact_by_value("Logseq", "笔记工具")
    assert fact["extract_sessions"] == ["self"], "self 去重 — 两批 bootstrap 仍 len=1"


# ── 4. M4 反向修正: 非 fallback replay 只写 seen_sessions ────────────

def test_nonfallback_replay_touches_seen_not_extract():
    """llm fact 的 recall_hits 重放: seen_sessions 吸收新 session,
    extract_sessions 分账列零变化 (使用痕迹 ≠ 提取证据)。"""
    tmp = _fresh("replay")
    restore_sig = _patch_signals(tmp / "signals")
    eid = store.put_entity("Logseq", "concept")
    fid = store.put_fact(eid, "is_a", "笔记工具", extractor="llm")
    conn = db.get_conn()
    conn.execute(
        "UPDATE fact SET seen_sessions = ?, extract_sessions = ?, "
        "lif_source = 0.7 WHERE id = ?",
        (json.dumps(["s1"]), json.dumps(["s1"]), fid))
    try:
        signals.append("recall_hits", {"fact_id": fid, "session_id": "s2",
                                       "query": "q", "score": 0.8})
        stats = dream._replay_recall_hits(None)
        assert stats["lif_facts"] == 1, stats
        fact = store.get_fact(fid)
        assert "s2" in (fact["seen_sessions"] or []), "重放写 seen_sessions"
        assert fact["extract_sessions"] == ["s1"], (
            "M4 反向修正: 重放不得写穿 extract_sessions 分账列")
        assert fact["access_count"] == 1, "非受限重放照旧 mild reinforcement"
    finally:
        restore_sig()


# ── 5. D6 暂缓期冻结 + 门默认关 ──────────────────────────────────────

def test_d6_gate_default_off_and_fallback_unconditional(monkeypatch):
    """env 默认关 = 暂缓期: 待验证 fact 不受限 (旧规则全额刷); 门开才受限。
    fallback 来源不受门控制 — 两个窗口都无条件受限 (⑤b 仅衰减不可提权)。"""
    monkeypatch.delenv(scoring.COLDSTART_UNLOCK_ENV, raising=False)
    assert scoring.coldstart_unlock_enabled() is False, "D6 门默认关"
    pending = {"extractor": "llm", "lif_source": 0.4,
               "extract_sessions": ["s1"]}
    assert scoring.fact_pending_verification(pending) is True
    assert scoring.refresh_restricted(pending) is False, (
        "暂缓期: 待验证不受限 (判据只写不读, 行为面冻结)")

    monkeypatch.setenv(scoring.COLDSTART_UNLOCK_ENV, "1")
    assert scoring.coldstart_unlock_enabled() is True
    assert scoring.refresh_restricted(pending) is True, (
        "解锁期: 待验证受限刷 (D6 双窗口切换)")

    fb = {"extractor": "regex", "lif_source": 0.4, "extract_sessions": []}
    monkeypatch.delenv(scoring.COLDSTART_UNLOCK_ENV, raising=False)
    assert scoring.refresh_restricted(fb) is True, "fallback 受限不受门控制"
    monkeypatch.setenv(scoring.COLDSTART_UNLOCK_ENV, "1")
    assert scoring.refresh_restricted(fb) is True, "fallback 受限两窗口恒定"


# ── 6. E4 dedup merge 并集 + 最高档 ─────────────────────────────────

def test_e4_merge_union_idempotent_and_highest_tier():
    """_merge_group: seen/extract 并集吸收 (dedup 不吞分账证据); survivor
    extractor 取 group 最高档 (human 0.9 > llm 0.7 > regex 0.4); 重跑幂等。"""
    _fresh("e4")
    eid = store.put_entity("Logseq", "concept")
    f_reg = store.put_fact(eid, "is_a", "笔记工具", extractor="regex",
                           source_refs=["session:s1"],
                           seen_sessions=["s1"], extract_sessions=["s1"])
    f_llm = store.put_fact(eid, "is_a", "笔记工具", extractor="llm",
                           source_refs=["session:s2"],
                           seen_sessions=["s2"], extract_sessions=["s2"])
    f_human = store.put_fact(eid, "is_a", "笔记工具", extractor="human",
                             source_refs=["session:s3"],
                             seen_sessions=["s3"], extract_sessions=["s3"])

    def _group():
        return [store.get_fact(f) for f in (f_reg, f_llm, f_human)]

    merged = consolidate._merge_group(_group())
    assert merged == 2, merged
    surv = store.get_fact(f_reg)
    assert surv["extractor"] == "human", (
        "E4 N4: survivor extractor = group 最高档 (信任就高不就低)")
    assert sorted(surv["extract_sessions"]) == ["s1", "s2", "s3"], (
        "extract_sessions 并集 — dedup 不吞解锁判据证据")
    assert sorted(surv["seen_sessions"]) == ["s1", "s2", "s3"]
    assert sorted(surv["source_refs"]) == ["session:s1", "session:s2",
                                           "session:s3"]
    for dup in (f_llm, f_human):
        row = store.get_fact(dup)
        assert row["status"] == "superseded" and row["supersedes_id"] == f_reg

    # 幂等: 同 group 重跑 → 并集不变, 不重置档位。
    consolidate._merge_group(_group())
    surv2 = store.get_fact(f_reg)
    assert surv2["extractor"] == "human"
    assert sorted(surv2["extract_sessions"]) == ["s1", "s2", "s3"]
    assert sorted(surv2["seen_sessions"]) == ["s1", "s2", "s3"]


# ── 7. 低初值 + 解锁期毕业 ───────────────────────────────────────────

def test_low_init_04_then_graduation_on_unlock(monkeypatch):
    """主径 llm brand-new ADD → lif_source=0.4 (待验证); 解锁期门开 + 第二
    个独立 session UPDATE (len=2) → 毕业到 extractor 真值档 0.7。"""
    tmp = _fresh("grad")
    tpath = _write_transcript(tmp / "t.jsonl", "Logseq 是笔记工具")
    monkeypatch.setattr(embedding, "embed", lambda text, providers=None: [])
    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", "llm")
    _mock_llm_extract(monkeypatch, "Logseq", "is_a", "笔记工具")
    monkeypatch.delenv(scoring.COLDSTART_UNLOCK_ENV, raising=False)

    out = autodream.autodream("s1", tpath)
    assert out["added"] == 1, out
    fact = _fact_by_value("Logseq", "笔记工具")
    assert fact["lif_source"] == scoring.LOW_INIT_LIF_SOURCE == 0.4, (
        "低初值裁决: 主径 ADD 写侧立即 lif_source=0.4")
    assert fact["extract_sessions"] == ["s1"]
    assert scoring.fact_pending_verification(fact) is True

    # 解锁期: 第二个独立主径 session → len=2 毕业。
    monkeypatch.setenv(scoring.COLDSTART_UNLOCK_ENV, "1")
    out2 = autodream.autodream("s2", tpath)
    assert out2["updated"] == 1, out2
    fact = _fact_by_value("Logseq", "笔记工具")
    assert sorted(fact["extract_sessions"]) == ["s1", "s2"]
    assert scoring.fact_unlocked(fact) is True
    assert fact["lif_source"] == scoring.SOURCE_WEIGHT["llm"], (
        "E5 毕业: lif_source 落 extractor 真值档")


# ── 8. E7 C1b 通道质量门槛矩阵 ───────────────────────────────────────

def test_e7_regex_challenger_noop_signal_enqueue(monkeypatch):
    """regex 挑战 llm (严格低档): NOOP 不 supersede + contradiction_pending
    信号 (ref/subject_id/predicate/old_value/new_value/channel) + segcontra:
    入队; done 后重入返 id (复活不返 None); 消费端走 rerun_segment 仲裁。"""
    tmp = _fresh("e7a")
    restore_sig = _patch_signals(tmp / "signals")
    tpath = _write_transcript(tmp / "t.jsonl", "Logseq 是笔记工具")
    monkeypatch.setattr(embedding, "embed", lambda text, providers=None: [])

    # 既有 llm 档 fact: Logseq is_a Obsidian (主径 ADD, s1)。
    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", "llm")
    _mock_llm_extract(monkeypatch, "Logseq", "is_a", "Obsidian")
    assert autodream.autodream("s1", tpath)["added"] == 1
    old = _fact_by_value("Logseq", "Obsidian")

    # regex 档挑战: Logseq 是笔记工具 (矛盾 judge 恒 True)。
    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", "regex")
    monkeypatch.setattr(autodream, "_judge_contradiction",
                        lambda *a, **k: True)
    out = autodream.autodream("s2", tpath, providers=[_FakeJudge()])
    try:
        assert out == {"added": 0, "updated": 0, "deleted": 0, "noop": 1}, out
        assert store.get_fact(old["id"])["status"] == "active", (
            "C1b: 低档挑战者不得处决高档 incumbent")
        sigs = signals.read("contradiction_pending")
        assert len(sigs) == 1, f"恰一条 pending 信号, got {sigs}"
        sig = sigs[0]
        for key in ("ref", "subject_id", "predicate", "old_value",
                    "new_value", "channel"):
            assert key in sig, f"信号缺字段 {key}: {sig}"
        assert sig["channel"] == "regex"
        assert sig["old_value"] == "Obsidian" and sig["new_value"] == "笔记工具"
        assert sig["ref"] == f"segcontra:{tpath}#seg0"
        assert sig["subject_id"] == old["subject_id"]

        conn = db.get_conn()
        rows = conn.execute(
            "SELECT id, status, attempts, material_ref FROM upgrade_queue"
            " WHERE material_ref LIKE 'segcontra:%'").fetchall()
        assert len(rows) == 1, rows
        assert rows[0]["status"] == "pending" and rows[0]["attempts"] == 0

        # done 后重入: 复活 (status pending + attempts=0), 返 id 不返 None。
        upgrade.mark_done(rows[0]["id"])
        rev = upgrade.enqueue_contra_segment(tpath, 0, "Logseq 是笔记工具")
        assert rev is not None, "done 项重入必须复活返 id"
        row = conn.execute(
            "SELECT status, attempts FROM upgrade_queue WHERE id = ?",
            (rev,)).fetchone()
        assert row["status"] == "pending" and row["attempts"] == 0

        # 消费端: segcontra 项走 rerun_segment 决策管道 (主径恢复 → 同档
        # 仲裁 supersede 照旧), 不是 ADD-only 直写; 旧 llm fact 被处决。
        monkeypatch.setenv("MEM_EXTRACT_CHANNEL", "llm")
        _mock_llm_extract(monkeypatch, "Logseq", "is_a", "笔记工具")
        stats = dream._consume_queue([])
        assert stats["queue_done"] == 1 and stats["queue_skipped"] == 0, stats
        assert stats["facts_upgraded"] >= 1, "rerun 产出 (ADD) 计入 facts_upgraded"
        assert store.get_fact(old["id"])["status"] == "superseded", (
            "主径仲裁重抽: 同档挑战 supersede 照旧")
    finally:
        restore_sig()


def test_e7_llm_challenger_supersede_unchanged(monkeypatch):
    """llm 挑战 regex (同档以上): supersede 照旧 — 旧 fact superseded、无
    contradiction_pending 信号、无 segcontra 入队。"""
    tmp = _fresh("e7b")
    restore_sig = _patch_signals(tmp / "signals")
    tpath = _write_transcript(tmp / "t.jsonl", "Logseq 是笔记工具")
    monkeypatch.setattr(embedding, "embed", lambda text, providers=None: [])

    # 既有 regex 档 fact (词典链 ADD)。
    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", "regex")
    out0 = autodream.autodream("s1", tpath)
    assert out0["added"] == 1, out0
    old = _fact_by_value("Logseq", "笔记工具")
    assert old["extractor"] == "regex"

    # llm 主径挑战: 值改 Obsidian (矛盾 judge 恒 True)。
    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", "llm")
    _mock_llm_extract(monkeypatch, "Logseq", "is_a", "Obsidian")
    monkeypatch.setattr(autodream, "_judge_contradiction",
                        lambda *a, **k: True)
    out = autodream.autodream("s2", tpath, providers=[_FakeJudge()])
    try:
        assert out["added"] == 1 and out["deleted"] == 1, out
        assert store.get_fact(old["id"])["status"] == "superseded", (
            "高档挑战者照旧 supersede (通道门槛不拦)")
        assert signals.read("contradiction_pending") == [], (
            "反向 (高 vs 低) 不发 pending 信号")
        rows = db.get_conn().execute(
            "SELECT id FROM upgrade_queue WHERE material_ref LIKE"
            " 'segcontra:%'").fetchall()
        assert rows == [], "llm 通道挑战不入队 segcontra"
    finally:
        restore_sig()


# ExtractFailed 可导入性自检 (E10 红线引用面, 防 import 漂移)。
def test_extract_failed_is_runtime_error():
    assert issubclass(ExtractFailed, RuntimeError)
