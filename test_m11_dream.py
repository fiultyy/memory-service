"""M11 dreaming 期批验收测试 (spec v2 §3 M11/M11-v2, DR-8 G8 载体已裁决)。

覆盖派发令五条验收:
1. 职责①: env=1 recall 信号 → run_cycle 批量补回 == env=0 直跑等价
   (access_count/last_accessed_at/LIF); 水位推进后二次 run_cycle 不重复消费。
2. 职责②: ephemeral 高 LIF → stable 晋升; 低 LIF 降级沿 decay (deprecated)。
3. 职责③④⑤: D9 diff 文件含前后值; 复述回流 fixture 压档; 自述污染 fixture
   标记降档 (fact_type→ephemeral + LIF 减半) 不物理删。
4. 职责⑥: monkeypatch wings — pending 2 项全 done、旧 fact
   supersede_reason='upgrade'、新 fact extractor 档位更新; wings 不可达 →
   队列回 pending 不 crash、其余职责照跑。
5. 幂等: 空信号空队列 run_cycle 全零不报错; daemon 门控 (mocker 不真跑循环)。

测试规范: def test_xxx() 函数让 pytest 收集。禁网络/LLM: adapter.extract_facts
monkeypatch; embedding.embed monkeypatch 可控向量; signals 目录指向 tmp。
"""
import os
import tempfile
import time
import uuid
from pathlib import Path

import adapter
import db
import dream
import embedding
import mem_daemon
import signals
import store
import upgrade
from llm_provider import EdgeOut, EntityOut, Extraction


def _fresh(name: str) -> tuple[str, Path]:
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / f"{name}.db")
    sig_dir = Path(tmp) / "signals"
    return tmp, sig_dir


def _patch_signals(sig_dir: Path):
    orig = signals._signals_dir
    signals._signals_dir = lambda: sig_dir
    return lambda: setattr(signals, "_signals_dir", orig)


def _seed_fact(value: str = "rust", **kw) -> str:
    eid = store.put_entity("用户" + uuid.uuid4().hex[:4], "inferred")
    return store.put_fact(eid, "uses", value, extractor="llm",
                          fact_type="permanent", source_cwd="/test",
                          LIF=0.6, confidence=0.8, topic="t", **kw)


def _recall_env1(query: str, fact_value: str) -> int:
    """env=1 跑一次 recall (产生信号, 零写回), 返回命中 fact_id。"""
    import recall as recall_mod
    fid = _seed_fact(fact_value)
    os.environ["MEM_DELAYED_REINFORCE"] = "1"
    try:
        res = recall_mod.recall(query, session_id="s1", top_k=5)
    finally:
        os.environ.pop("MEM_DELAYED_REINFORCE", None)
    hits = [f["id"] for f in res if f["id"] == fid]
    assert hits, "fixture: recall 必须命中 seed fact"
    return fid


# ── 验收 1: 职责① 批量补回等价 + 水位 ────────────────────────────────

def test_lif_replay_equivalent_to_immediate_writeback():
    """双库对照: A 库 env=0 直跑 recall (即时写回); B 库 env=1 recall + run_cycle
    (批量补回)。终态 access_count 相等、last_accessed_at 均落值、LIF 等价
    (容差吸收秒级墙钟差)。"""
    import recall as recall_mod
    # A 库: 即时写回路径。
    tmp_a, sig_a = _fresh("envA")
    restore_a = _patch_signals(sig_a)
    try:
        fid_a = _seed_fact("rust")
        os.environ.pop("MEM_DELAYED_REINFORCE", None)
        recall_mod.recall("rust", session_id="s1", top_k=5)
        fa = store.get_fact(fid_a)
    finally:
        restore_a()
    # B 库: 改道路径 + dreaming 批量补回。
    tmp_b, sig_b = _fresh("envB")
    restore_b = _patch_signals(sig_b)
    orig_embed = embedding.embed
    embedding.embed = lambda text, providers=None: []  # reflux 离线跳过
    try:
        fid_b = _seed_fact("rust")
        os.environ["MEM_DELAYED_REINFORCE"] = "1"
        try:
            recall_mod.recall("rust", session_id="s1", top_k=5)
        finally:
            os.environ.pop("MEM_DELAYED_REINFORCE", None)
        stats = dream.run_cycle()
        fb = store.get_fact(fid_b)
    finally:
        embedding.embed = orig_embed
        restore_b()

    assert stats["signals_consumed"] >= 1 and stats["lif_facts"] >= 1, stats
    assert fb["access_count"] == fa["access_count"] == 1, (
        f"补回应等价: A={fa['access_count']} B={fb['access_count']}")
    assert fb["last_accessed_at"] and fa["last_accessed_at"]
    assert abs(fb["LIF"] - fa["LIF"]) < 0.05, (
        f"LIF 等价 (容差吸收秒级墙钟): A={fa['LIF']} B={fb['LIF']}")
    assert "s1" in fb["seen_sessions"]


def test_watermark_no_double_consume():
    """水位推进: 二次 run_cycle 零新消费 — access_count 不再涨。"""
    tmp, sig = _fresh("wm")
    restore = _patch_signals(sig)
    orig_embed = embedding.embed
    embedding.embed = lambda text, providers=None: []
    try:
        fid = _recall_env1("rust", "rust")
        s1 = dream.run_cycle()
        assert s1["signals_consumed"] >= 1
        s2 = dream.run_cycle()
        assert s2["signals_consumed"] == 0 and s2["lif_facts"] == 0, (
            f"水位后不得重复消费, got {s2}")
        assert store.get_fact(fid)["access_count"] == 1, "二次消费会双计 access"
    finally:
        embedding.embed = orig_embed
        restore()


# ── 验收 2: 职责② 晋升 / decay 降级 ──────────────────────────────────

def test_promotion_ephemeral_high_lif():
    tmp, sig = _fresh("promo")
    restore = _patch_signals(sig)
    try:
        eid = store.put_entity("高价值", "inferred")
        fid = store.put_fact(eid, "is_a", "high", extractor="human",
                             fact_type="ephemeral", LIF=0.85, lif_source=0.9)
        stats = dream.run_cycle()
        assert stats["promoted"] >= 1, stats
        assert store.get_fact(fid)["fact_type"] == "stable", "ephemeral 高 LIF 应晋升 stable"
    finally:
        restore()


def test_demotion_follows_decay():
    """降级沿 decay 语义: decay 从五维**重算** LIF。重算下限 = coherence 0.5
    (冲突对按类型去重, 单对封顶 1/2) + source 档 — regex 0.4 档下限 0.135 打不破
    0.1 阈值, 故走 consolidate 冻结的 legacy 通道: extractor='' →
    source_override=original_lif (ADR-8v2 老数据回退)。fixture: uses/avoids 互斥
    对 (coherence 0.5) + 远古 last_accessed_at (ephemeral 7d 半衰期→recency 0)
    + original_lif 0.1 (source 0.1) → 重算 LIF=0.09 < 0.1 → deprecated。"""
    tmp, sig = _fresh("demo")
    restore = _patch_signals(sig)
    try:
        eid = store.put_entity("低价值", "inferred")
        ancient = "2023-01-01T00:00:00+00:00"
        fid = None
        for pred, val in (("uses", "v1"), ("avoids", "v1")):
            fid = store.put_fact(eid, pred, val, extractor="",
                                 fact_type="ephemeral", original_lif=0.1,
                                 last_accessed_at=ancient)
        stats = dream.run_cycle()
        assert stats["deprecated"] >= 1, stats
        row = db.get_conn().execute(
            "SELECT status FROM fact WHERE id=?", (fid,)).fetchone()
        assert row["status"] == "deprecated", "低 LIF 沿 decay 语义降级"
    finally:
        restore()


# ── 验收 3: 职责③ D9 diff / ④ 回流压档 / ⑤ 自述污染 ──────────────────

def test_d9_param_diff_file():
    """6 条 regex fact, 3 条被 upgrade supersede → 升级率 0.5 ≥ 阈值 →
    diff 文件生成, 含 before/after 值 (0.4 → 0.45)。"""
    tmp, sig = _fresh("d9")
    restore = _patch_signals(sig)
    try:
        eid = store.put_entity("S", "concept")
        for i in range(6):
            fid = store.put_fact(eid, "uses", f"v{i}", extractor="regex")
            if i < 3:
                new = store.put_fact(eid, "uses", f"v{i}-up", extractor="vote")
                store.update_fact_status(fid, "superseded", supersedes_id=new,
                                         valid_to=store._now(), reason="upgrade")
        stats = dream.run_cycle()
        assert stats["param_proposals"] >= 1, stats
        diffs = list(sig.parent.glob("param-diff-*.md"))
        assert diffs, "D9 diff 文件应生成"
        content = diffs[0].read_text("utf-8")
        assert "regex" in content and "0.4" in content and "0.45" in content, (
            f"diff 应含 extractor 前后值:\n{content}")
        # 不静默改: SOURCE_WEIGHT 运行时值原样。
        import scoring
        assert scoring.SOURCE_WEIGHT["regex"] == 0.4
    finally:
        restore()


def test_reflux_suppression():
    """复述回流: 高频命中 fact (2 hits) value 向量 == pending 素材向量 →
    cosine 1 > 0.92 → surprise/priority 压半档; 不相似 pending 项不动。"""
    tmp, sig = _fresh("reflux")
    restore_sig = _patch_signals(sig)
    _V_A, _V_B = [1.0, 0.0], [0.0, 1.0]
    orig_embed = embedding.embed
    embedding.embed = lambda text, providers=None: (
        _V_A if "echo duplicate" in text
        else (_V_B if "brand new" in text else []))
    try:
        fid = _seed_fact("echo duplicate value")
        # 高频命中: 手写 2 条 recall_hits 信号 (fact 出现 ≥ REFLUX_MIN_HITS)。
        signals.append("recall_hits", {"fact_id": fid, "session_id": "s",
                                       "query": "q", "score": 0.9,
                                       "source_cwd": None})
        signals.append("recall_hits", {"fact_id": fid, "session_id": "s",
                                       "query": "q", "score": 0.9,
                                       "source_cwd": None})
        # 两个 pending 素材: 一个回流 (向量同 A), 一个全新 (向量 B)。
        q_echo = upgrade.enqueue("reflux:echo", text="echo duplicate text")
        q_new = upgrade.enqueue("reflux:new", text="brand new text")
        # 先消费掉 recall_hits 水位, 避免职责①把 fact 强化。
        dream._replay_recall_hits(None)
        conn = db.get_conn()
        conn.execute("UPDATE fact SET access_count=0 WHERE id=?", (fid,))
        conn.commit()
        stats = dream.run_cycle()
        assert stats["reflux_suppressed"] == 1, stats
        rows = {r["material_ref"]: r for r in conn.execute(
            "SELECT material_ref, surprise, priority FROM upgrade_queue")}
        assert rows["reflux:echo"]["priority"] == 0.0, (
            "回流项 priority 应被压档 (novelty 0 → 0×0.5=0)")
        assert rows["reflux:new"]["priority"] == 1.0, (
            "不相似项 (novelty 1) 不应被压档")
    finally:
        embedding.embed = orig_embed
        restore_sig()


def test_reflux_suppression_online_vectors():
    """在线向量版回流断言: embed 可控 (novelty 高) — 回流项 priority 减半,
    不相似项 priority 保持。"""
    tmp, sig = _fresh("reflux2")
    restore_sig = _patch_signals(sig)
    _V_A, _V_B = [1.0, 0.0], [0.0, 1.0]
    state = {"n": 0}
    orig_embed = embedding.embed

    def _embed(text, providers=None):
        if "echo dup" in text:
            return list(_V_A)
        if "fresh thing" in text:
            return list(_V_B)
        return []

    embedding.embed = _embed
    try:
        fid = _seed_fact("totally different baseline")
        # 让 fact value 与回流素材同向量: value 文本含 'echo dup'。
        conn = db.get_conn()
        conn.execute("UPDATE fact SET value='echo dup value' WHERE id=?", (fid,))
        conn.commit()
        signals.append("recall_hits", {"fact_id": fid, "session_id": "s",
                                       "query": "q", "score": 0.9})
        signals.append("recall_hits", {"fact_id": fid, "session_id": "s",
                                       "query": "q", "score": 0.9})
        # KG 既有 value 占位 (novelty 计算用): 与素材不同向量 → novelty 高 → priority>0。
        store.put_fact(store.put_entity("Base", "concept"), "is_a", "zzz base",
                       extractor="llm")
        upgrade.enqueue("r:echo", text="echo dup material")
        upgrade.enqueue("r:fresh", text="fresh thing material")
        stats = dream.run_cycle()
        assert stats["reflux_suppressed"] == 1, stats
        rows = {r["material_ref"]: (r["surprise"], r["priority"])
                for r in db.get_conn().execute(
                    "SELECT material_ref, surprise, priority FROM upgrade_queue")}
        # 回流项: 在线 novelty 1 → surprise≈1+ε, priority 压半 (≤0.6);
        # 新项: surprise≈1, priority 原样 (>0.9)。
        assert rows["r:echo"][1] < 0.7, f"回流项应压档: {rows}"
        assert rows["r:fresh"][1] > 0.9, f"不相似项不应压: {rows}"
    finally:
        embedding.embed = orig_embed
        restore_sig()


def test_self_pollution_demoted_not_deleted():
    tmp, sig = _fresh("pollute")
    restore = _patch_signals(sig)
    try:
        eid = store.put_entity("P", "concept")
        clean = store.put_fact(eid, "uses", "rust 工具链", extractor="llm",
                               fact_type="stable", LIF=0.8)
        dirty = store.put_fact(eid, "is_a", "我记得之前说过 rust 很好用",
                               extractor="llm", fact_type="stable", LIF=0.8)
        stats = dream.run_cycle()
        assert stats["pollution_demoted"] == 1, stats
        fd = store.get_fact(dirty)
        fc = store.get_fact(clean)
        assert fd is not None, "自述污染不物理删"
        assert fd["fact_type"] == "ephemeral", "标记降档: fact_type→ephemeral"
        assert fd["LIF"] < fc["LIF"], (
            f"降档: 污染 fact LIF ({fd['LIF']}) 应低于干净 fact ({fc['LIF']})")
        assert fc["fact_type"] == "stable", "干净 fact 不误伤 (档位不动)"
    finally:
        restore()


# ── 验收 4: 职责⑥ wings 队列消费 ────────────────────────────────────

def _fake_wings_ok(text, providers=None):
    return Extraction(
        entities=[EntityOut("用户", "person"), EntityOut("rust", "tool")],
        edges=[EdgeOut("用户", "uses", "rust", topic="用户使用 rust")],
        confidence=0.9,
        source_meta={"provider": "fake", "extractor_label": "vote"})


def test_queue_wings_upgrade_success():
    """pending 2 项 (占位 fact) → run_cycle 全 done; 旧 fact
    supersede_reason='upgrade' + status='superseded'; 新 fact extractor='vote'。"""
    tmp, sig = _fresh("wingsok")
    restore = _patch_signals(sig)
    orig_extract, orig_embed = adapter.extract_facts, embedding.embed
    adapter.extract_facts = _fake_wings_ok
    embedding.embed = lambda text, providers=None: []
    try:
        eid = store.put_entity("用户", "person")
        f1 = store.put_fact(eid, "uses", "rust", extractor="regex")
        f2 = store.put_fact(eid, "is_a", "tool", extractor="regex")
        upgrade.enqueue_fact(f1, subject="用户", predicate="uses", obj="rust")
        upgrade.enqueue_fact(f2, subject="用户", predicate="is_a", obj="tool")
        stats = dream.run_cycle()
        assert stats["queue_done"] == 2 and stats["facts_upgraded"] == 2, stats
        conn = db.get_conn()
        for fid in (f1, f2):
            old = conn.execute(
                "SELECT status, supersede_reason, supersedes_id FROM fact "
                "WHERE id=?", (fid,)).fetchone()
            assert old["status"] == "superseded"
            assert old["supersede_reason"] == "upgrade", (
                f"升级 supersede 必须带 reason, got {old['supersede_reason']}")
            new = conn.execute(
                "SELECT extractor, status, provenance FROM fact WHERE id=?",
                (old["supersedes_id"],)).fetchone()
            assert new["extractor"] == "vote", "新 fact extractor 档位更新 (wings)"
            assert new["status"] == "active"
        n_done = conn.execute(
            "SELECT COUNT(*) FROM upgrade_queue WHERE status='done'").fetchone()[0]
        assert n_done == 2
    finally:
        adapter.extract_facts = orig_extract
        embedding.embed = orig_embed
        restore()


def test_queue_wings_unreachable_skip_round():
    """wings 不可达 (RuntimeError): 整轮跳过 — 队列全回 pending (attempts 0),
    无 crash, 其余职责照跑 (信号重放/晋升)。"""
    tmp, sig = _fresh("wingsdown")
    restore = _patch_signals(sig)
    orig_extract, orig_embed = adapter.extract_facts, embedding.embed

    def _boom(text, providers=None):
        raise RuntimeError("no reachable LLM provider — regex fallback removed")

    adapter.extract_facts = _boom
    embedding.embed = lambda text, providers=None: []
    try:
        eid = store.put_entity("用户", "person")
        f1 = store.put_fact(eid, "uses", "rust", extractor="regex")
        f2 = store.put_fact(eid, "is_a", "tool", extractor="regex")
        upgrade.enqueue_fact(f1, subject="用户", predicate="uses", obj="rust")
        upgrade.enqueue_fact(f2, subject="用户", predicate="is_a", obj="tool")
        # 同轮信号重放应照跑 (其余职责不受 wings 断供影响)。
        signals.append("recall_hits", {"fact_id": f1, "session_id": "sx",
                                       "query": "q", "score": 0.8})
        stats = dream.run_cycle()  # 不 raise = 验收一半
        assert stats["queue_skipped"] == 2 and stats["queue_done"] == 0, stats
        assert stats["lif_facts"] == 1, "其余职责照跑 (信号重放发生)"
        rows = db.get_conn().execute(
            "SELECT status, attempts FROM upgrade_queue").fetchall()
        assert all(r["status"] == "pending" and r["attempts"] == 0 for r in rows), (
            f"整轮回退 pending 且 attempts 不烧: {[(r['status'], r['attempts']) for r in rows]}")
        assert store.get_fact(f1)["access_count"] == 1, "重放补回发生"
    finally:
        adapter.extract_facts = orig_extract
        embedding.embed = orig_embed
        restore()


# ── 验收 5: 空轮幂等 + daemon 门控 ────────────────────────────────────

def test_empty_cycle_all_zero_no_error():
    tmp, sig = _fresh("empty")
    restore = _patch_signals(sig)
    orig_embed = embedding.embed
    embedding.embed = lambda text, providers=None: []
    try:
        stats = dream.run_cycle()
        assert stats["signals_consumed"] == 0 and stats["lif_facts"] == 0
        assert stats["promoted"] == 0 and stats["param_proposals"] == 0
        assert stats["reflux_suppressed"] == 0 and stats["pollution_demoted"] == 0
        assert stats["queue_done"] == 0 and stats["queue_skipped"] == 0
    finally:
        embedding.embed = orig_embed
        restore()


def test_daemon_dream_gate_interval():
    """门控: 未到期不跑 (stats 空); 到期跑且异常不杀 daemon (继续返回 state)。"""
    state = {}
    calls = []

    def _fake_cycle(providers=None, source_cwd=None):
        calls.append(source_cwd)
        return {"lif_facts": 0}

    orig_cycle, orig_interval = dream.run_cycle, mem_daemon._DREAM_INTERVAL
    dream.run_cycle = _fake_cycle
    mem_daemon._DREAM_INTERVAL = 86400
    try:
        # last_run 刚跑过 (now-1) → 未到期, 不触发。
        state = {"_dreaming": {"last_run": time.time() - 1}}
        out = mem_daemon._maybe_dream(state, "/w")
        assert calls == [], "未到期不得触发 dreaming"
        assert out is state
        # 到期 (now-间隔-1) → 触发, state 记 last_run。
        state = {"_dreaming": {"last_run": time.time() - 86401}}
        out = mem_daemon._maybe_dream(state, "/w")
        assert calls == ["/w"], calls
        assert "_dreaming" in out and out["_dreaming"]["last_run"] >= time.time() - 5
        # run_cycle 抛异常 → 不传播 (daemon 不死), state 仍推进 last_run。
        calls.clear()
        def _bad_cycle(providers=None, source_cwd=None):
            raise RuntimeError("boom")
        dream.run_cycle = _bad_cycle
        out = mem_daemon._maybe_dream({"_dreaming": {"last_run": 0}}, "/w")
        assert "_dreaming" in out, "异常轮也推进 last_run (防死循环重试)"
    finally:
        dream.run_cycle = orig_cycle
        mem_daemon._DREAM_INTERVAL = orig_interval
