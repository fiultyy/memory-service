"""M12 投影卫生 + M13 BFS 门槛批验收测试 (spec v2 §3 M12/M13, G5 已裁决)。

覆盖派发令四条验收:
1. M12 去重: superseded fact 投影退场; active fact 投影保留。
2. M12 重排/裁剪: [mem] 段序按 KG 现值重写; deprecated 投影退场; 零 LLM 断言
   (adapter/gazetteer 提取调用计数=0)。
3. M12 时序: dream 到期轮卫生同轮紧随; dream 未到期卫生独立门控; 卫生异常
   不杀 daemon。
4. M13 门槛: regex(0.4) fact 不入 BFS 扩展; llm(0.7) 入; hop>0 bypass 仅高分
   邻域生效; 主检索路径 (字面 seed) 不受门槛影响。

测试规范: def test_xxx() 函数让 pytest 收集。禁网络/LLM。
"""
import tempfile
import time
from pathlib import Path

import adapter
import db
import gazetteer
import hygiene
import mem_daemon
import projection
import recall as recall_mod
import store


def _fresh(name: str) -> tuple[str, Path]:
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / f"{name}.db")
    mem_dir = Path(tmp) / "memory"
    mem_dir.mkdir()
    # 隔离信号目录 (M18 接线后 hygiene.run 首步跑投影 diff 检测 → 写
    # human_proj_ops 流; 无 patch 会泄漏到默认 data/signals — 冒烟发现的
    # 隔离缺口, 照 test_m16_m18_tools fixture 模式)。
    global _SIG_RESTORE
    import signals
    _orig = signals._signals_dir
    signals._signals_dir = lambda: Path(tmp) / "signals"
    _SIG_RESTORE = lambda: setattr(signals, "_signals_dir", _orig)
    return tmp, mem_dir


_SIG_RESTORE = None


def _restore_signals():
    if _SIG_RESTORE is not None:
        _SIG_RESTORE()
        globals()["_SIG_RESTORE"] = None


def _project(fid: str, mem_dir: Path) -> Path:
    """用投影写入口为 fact 建 mem-*.md (recall 同路径), 返回文件路径。"""
    f = store.get_fact(fid)
    eid = db.get_conn().execute(
        "SELECT name FROM entity WHERE id=?", (f["subject_id"],)).fetchone()
    projection.project_fact_md(f, eid["name"], mem_dir)
    return mem_dir / projection._mem_filename(fid, projection._fact_topic(f, eid["name"]))


# ── 验收 1: M12 去重 ─────────────────────────────────────────────────

def test_dedup_removes_superseded_projection():
    tmp, mem_dir = _fresh("dedup")
    eid = store.put_entity("S", "concept")
    keep = store.put_fact(eid, "uses", "active fact value", extractor="llm",
                          fact_type="permanent", LIF=0.6, topic="keep")
    gone = store.put_fact(eid, "is_a", "superseded fact value", extractor="llm",
                          fact_type="permanent", LIF=0.6, topic="gone")
    new = store.put_fact(eid, "is_a", "upgraded value", extractor="vote",
                         fact_type="permanent", LIF=0.6, topic="new")
    store.update_fact_status(gone, "superseded", supersedes_id=new,
                             valid_to=store._now(), reason="upgrade")
    p_keep, p_gone = _project(keep, mem_dir), _project(gone, mem_dir)
    assert p_gone.is_file()

    stats = hygiene.run("/w", mem_dir)
    assert stats["dedup_removed"] == 1, stats
    assert not p_gone.exists(), "superseded fact 投影必须退场"
    assert p_keep.exists(), "active fact 投影保留"
    _restore_signals()


# ── 验收 2: M12 裁剪 + 重排 + 零 LLM ─────────────────────────────────

def test_prune_removes_deprecated_projection():
    tmp, mem_dir = _fresh("prune")
    eid = store.put_entity("S", "concept")
    active = store.put_fact(eid, "uses", "live value", extractor="llm",
                            fact_type="permanent", LIF=0.6, topic="live")
    dead = store.put_fact(eid, "is_a", "stale value", extractor="llm",
                          fact_type="permanent", LIF=0.6, topic="stale")
    store.update_fact_status(dead, "deprecated", valid_to=store._now())
    p_active, p_dead = _project(active, mem_dir), _project(dead, mem_dir)

    stats = hygiene.run("/w", mem_dir)
    assert stats["prune_removed"] == 1, stats
    assert not p_dead.exists(), "deprecated fact 投影退场 (出热区)"
    assert p_active.exists()
    _restore_signals()


def test_resort_mem_section_order_follows_current_values():
    """][mem] 段序按 KG 现值重写: 两个 fact 投影后反转 LIF → 卫生轮后
    MEMORY.md 行序翻转 (mem_score 主导, scoring.env 缺省 weighted 0.7 LIF)。"""
    tmp, mem_dir = _fresh("resort")
    eid = store.put_entity("S", "concept")
    low = store.put_fact(eid, "uses", "low value", extractor="llm",
                         fact_type="permanent", LIF=0.3, confidence=0.5,
                         topic="low")
    high = store.put_fact(eid, "is_a", "high value", extractor="llm",
                          fact_type="permanent", LIF=0.9, confidence=0.9,
                          topic="high")
    _project(low, mem_dir)
    _project(high, mem_dir)

    hygiene.run("/w", mem_dir)  # 首轮: high 在前
    lines = [l for l in (mem_dir / "MEMORY.md").read_text("utf-8").splitlines()
             if projection._is_mem_index_line(l)]
    assert len(lines) == 2, lines
    assert "high" in lines[0] and "low" in lines[1], f"应按现值降序: {lines}"

    # 反转 LIF → 重排翻转。
    conn = db.get_conn()
    conn.execute("UPDATE fact SET LIF=0.95 WHERE id=?", (low,))
    conn.execute("UPDATE fact SET LIF=0.10 WHERE id=?", (high,))
    conn.commit()
    hygiene.run("/w", mem_dir)
    lines2 = [l for l in (mem_dir / "MEMORY.md").read_text("utf-8").splitlines()
              if projection._is_mem_index_line(l)]
    assert "low" in lines2[0] and "high" in lines2[1], (
        f"LIF 反转后段序应翻转: {lines2}")
    _restore_signals()


def test_hygiene_zero_llm():
    """零 LLM 断言: monkeypatch adapter.extract_facts / gazetteer.extract 计数,
    hygiene.run 全程调用数 = 0。"""
    tmp, mem_dir = _fresh("zerollm")
    eid = store.put_entity("S", "concept")
    fid = store.put_fact(eid, "uses", "v", extractor="llm",
                         fact_type="permanent", LIF=0.6, topic="t")
    _project(fid, mem_dir)
    calls = {"adapter": 0, "gazetteer": 0}
    orig_a, orig_g = adapter.extract_facts, gazetteer.extract

    def _count_a(*a, **k):
        calls["adapter"] += 1
        raise AssertionError("hygiene 不得调 adapter (零 LLM)")

    def _count_g(*a, **k):
        calls["gazetteer"] += 1
        raise AssertionError("hygiene 不得调 gazetteer (零 LLM)")

    adapter.extract_facts = _count_a
    gazetteer.extract = _count_g
    try:
        hygiene.run("/w", mem_dir)
    finally:
        adapter.extract_facts = orig_a
        gazetteer.extract = orig_g
    assert calls == {"adapter": 0, "gazetteer": 0}
    _restore_signals()


# ── 验收 3: M12 daemon 时序 ──────────────────────────────────────────

def test_daemon_dream_due_hygiene_same_round():
    """dream 到期 → 卫生同轮紧随 (时序铁律: KG 维护后才跑)。"""
    import dream
    order = []
    orig_dream, orig_hyg = dream.run_cycle, hygiene.run
    dream.run_cycle = lambda **k: order.append("dream") or {}
    hygiene.run = lambda cwd, mem_dir: order.append("hygiene") or {
        "dedup_removed": 0, "prune_removed": 0, "resorted": 0}
    orig_di, orig_hi = mem_daemon._DREAM_INTERVAL, mem_daemon._HYGIENE_INTERVAL
    mem_daemon._DREAM_INTERVAL = 86400
    mem_daemon._HYGIENE_INTERVAL = 86400
    try:
        state = mem_daemon._maybe_dream({"_dreaming": {"last_run": 0}}, "/w")
        assert order == ["dream", "hygiene"], f"卫生应同轮紧随 dream: {order}"
        assert "_dreaming" in state and "_hygiene" in state
    finally:
        dream.run_cycle = orig_dream
        hygiene.run = orig_hyg
        mem_daemon._DREAM_INTERVAL = orig_di
        mem_daemon._HYGIENE_INTERVAL = orig_hi


def test_daemon_hygiene_independent_gate():
    """dream 未到期 + 卫生自身门控到点 → 卫生独立跑 (dream 不跑);
    两者都未到点 → 全不跑。"""
    import dream
    order = []
    orig_dream, orig_hyg = dream.run_cycle, hygiene.run
    dream.run_cycle = lambda **k: order.append("dream") or {}
    hygiene.run = lambda cwd, mem_dir: order.append("hygiene") or {
        "dedup_removed": 0, "prune_removed": 0, "resorted": 0}
    orig_di, orig_hi = mem_daemon._DREAM_INTERVAL, mem_daemon._HYGIENE_INTERVAL
    mem_daemon._DREAM_INTERVAL = 86400
    mem_daemon._HYGIENE_INTERVAL = 3600
    try:
        # dream 刚跑过 (未到期), 卫生从未跑 (到点) → 只跑卫生。
        state = {"_dreaming": {"last_run": time.time() - 10}}
        out = mem_daemon._maybe_dream(state, "/w")
        assert order == ["hygiene"], f"卫生应独立门控跑: {order}"
        assert "_hygiene" in out
        # 都未到点 → 全不跑。
        order.clear()
        state = {"_dreaming": {"last_run": time.time() - 10},
                 "_hygiene": {"last_run": time.time() - 10}}
        mem_daemon._maybe_dream(state, "/w")
        assert order == [], f"未到点不得跑: {order}"
    finally:
        dream.run_cycle = orig_dream
        hygiene.run = orig_hyg
        mem_daemon._DREAM_INTERVAL = orig_di
        mem_daemon._HYGIENE_INTERVAL = orig_hi


def test_daemon_hygiene_exception_does_not_kill():
    """卫生抛异常 → 不传播 (daemon 不死), last_run 仍推进。"""
    import dream
    orig_hyg = hygiene.run
    hygiene.run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    orig_di = mem_daemon._DREAM_INTERVAL
    mem_daemon._DREAM_INTERVAL = 86400
    try:
        state = {"_dreaming": {"last_run": time.time() - 10},
                 "_hygiene": {"last_run": 0}}
        mem_daemon._DREAM_INTERVAL = 86400
        mem_daemon._HYGIENE_INTERVAL = 0
        out = mem_daemon._maybe_dream(state, "/w")  # 不 raise
        assert "_hygiene" in out
    finally:
        hygiene.run = orig_hyg
        mem_daemon._DREAM_INTERVAL = orig_di


# ── 验收 4: M13 BFS 门槛 ─────────────────────────────────────────────

def _bfs_chain(extractor_bc: str, extractor_cd: str):
    """A --uses(llm)--> B --runs_on(extractor_bc)--> C --depends_on(
    extractor_cd)--> D 链 (循 test_bfs_recall fixture 惯例)。"""
    ea = store.put_entity("Alpha", "concept")
    eb = store.put_entity("Bravo", "concept")
    ec = store.put_entity("Charlie", "concept")
    ed = store.put_entity("Delta", "concept")
    store.put_fact(ea, "uses", "Alpha uses Bravo", extractor="llm",
                   fact_type="permanent", LIF=0.5, confidence=0.8,
                   topic="A uses B", object_id=eb)
    f_bc = store.put_fact(eb, "runs_on", "Bravo runs on Charlie",
                          extractor=extractor_bc, fact_type="permanent",
                          LIF=0.5, confidence=0.8, topic="B runs on C",
                          object_id=ec)
    f_cd = store.put_fact(ec, "depends_on", "Charlie depends on Delta",
                          extractor=extractor_cd, fact_type="permanent",
                          LIF=0.5, confidence=0.8, topic="C depends on D",
                          object_id=ed)
    return f_bc, f_cd


def test_bfs_gate_regex_rejected_llm_admitted():
    """regex 档 (lif_source 0.4) 邻居 fact 不入 BFS 扩展; llm 档 (0.7) 入。"""
    tmp, _ = _fresh("gate1")
    f_bc, f_cd = _bfs_chain("regex", "regex")
    res = recall_mod.recall("Alpha", use_bfs=True, bfs_hops=2, boost=False)
    ids = {f["id"] for f in res}
    assert f_bc not in ids and f_cd not in ids, (
        f"regex 0.4 档不得经 BFS 扩展入场, got {ids}")

    tmp2, _ = _fresh("gate2")
    f_bc2, f_cd2 = _bfs_chain("llm", "llm")
    res2 = recall_mod.recall("Alpha", use_bfs=True, bfs_hops=2, boost=False)
    ids2 = {f["id"] for f in res2}
    assert f_bc2 in ids2 and f_cd2 in ids2, (
        f"llm 0.7 档应正常入场 (既有行为), got {ids2}")
    _restore_signals()


def test_bfs_gate_mixed_tiers():
    """混合档: B→C 边 llm (B 的 fact 入场), C→D 边 regex (C 的 fact 拒) —
    门槛按 fact 自身档位逐条判。"""
    tmp, _ = _fresh("gate3")
    f_bc, f_cd = _bfs_chain("llm", "regex")
    res = recall_mod.recall("Alpha", use_bfs=True, bfs_hops=2, boost=False)
    ids = {f["id"] for f in res}
    assert f_bc in ids, "llm 档邻居 fact 应入场"
    assert f_cd not in ids, "regex 档邻居 fact 应拒"
    _restore_signals()


def test_bfs_bypass_only_within_gated_neighborhood():
    """hop>0 bypass (绕 0.3 过滤) 仅对过门槛入场者生效: llm 档低分 fact
    仍被 bypass 召回 (§7 语义保留); regex 档低分 fact 不因 bypass 入图。"""
    tmp, _ = _fresh("gate4")
    # B 的 fact: llm 档, match 分极低 (value 与 query 无字面重叠) → 靠 bypass。
    ea = store.put_entity("Alpha", "concept")
    eb = store.put_entity("Bravo", "concept")
    ec = store.put_entity("Charlie", "concept")
    ed = store.put_entity("Delta", "concept")
    store.put_fact(ea, "uses", "Alpha uses Bravo", extractor="llm",
                   fact_type="permanent", LIF=0.5, confidence=0.8,
                   object_id=eb, topic="AB")
    f_low_llm = store.put_fact(eb, "runs_on", "zzz qqq xxx", extractor="llm",
                               fact_type="permanent", LIF=0.5, confidence=0.8,
                               object_id=ec, topic="lowmatch llm")
    # 低分 regex fact: 两端都不沾 seed (沾 seed 会走主路径, 门槛不管主路径)。
    f_low_regex = store.put_fact(ec, "depends_on", "yyy www vvv",
                                 extractor="regex", fact_type="permanent",
                                 LIF=0.5, confidence=0.8, object_id=ed,
                                 topic="lowmatch regex")
    res = recall_mod.recall("Alpha", use_bfs=True, bfs_hops=2, boost=False)
    ids = {f["id"] for f in res}
    assert f_low_llm in ids, (
        f"llm 档扩展 fact 应享 hop>0 bypass (低 match 仍入场): {ids}")
    assert f_low_regex not in ids, (
        f"regex 档不得因 bypass 入图: {ids}")
    _restore_signals()


def test_main_retrieval_path_ungated():
    """主检索路径不受门槛: regex 档 fact 仍可被直接查询召回 (字面 seed 命中
    其 subject 实体 → 主路径候选, 非 BFS 扩展通道)。"""
    tmp, _ = _fresh("gate5")
    eid = store.put_entity("Zephyr", "concept")
    f_regex = store.put_fact(eid, "is_a", "zephyr tool fact", extractor="regex",
                             fact_type="permanent", LIF=0.5, confidence=0.8,
                             topic="zephyr")
    res = recall_mod.recall("Zephyr", boost=False)
    ids = {f["id"] for f in res}
    assert f_regex in ids, (
        f"主路径 (字面 seed) 不得受 BFS 门槛影响: {ids}")
    # 对照: 同 fact 经 BFS 扩展通道 (从别的 seed 走图过来) 被拒。
    ea = store.put_entity("AnchorEnt", "concept")
    store.put_fact(ea, "uses", "AnchorEnt uses Zephyr", extractor="llm",
                   fact_type="permanent", LIF=0.5, confidence=0.8,
                   object_id=eid, topic="anchor")
    res2 = recall_mod.recall("AnchorEnt", use_bfs=True, bfs_hops=2, boost=False)
    ids2 = {f["id"] for f in res2}
    assert f_regex not in ids2, "同 fact 经扩展通道必须被门槛拒"
    _restore_signals()
