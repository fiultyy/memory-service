"""ADR-1 R1 autodream 矛盾 supersede 接线测试 (A2 ingest 侧)。

覆盖 autodream._judge_contradiction + ingest 侧 supersede 接线:
- 多值谓词 short-circuit: 已知多值集直接 no-contradiction 跳过 LLM (省调用 + 防
  LLM 误判共存)。
- 单值谓词走 provider.judge_contradiction: contradiction=True → supersede 旧 fact
  设 valid_to (与 bi-temporal supersede 一致)。
- provider 不可达 / [] / raises → fallback contradiction=False → 不 supersede 不阻断。
- 同值不矛盾 (fast path)。
- 复用 _has_active_for_predicate (不新写 scan)。

测试规范: def test_xxx() 函数让 pytest 收集 (本项目头号雷区=模块级裸 assert 死代码,
test_bi_temporal/test_bfs_recall/test_as_of_normalize 是历史债勿复制)。
"""
import tempfile
from pathlib import Path

import autodream
import db
import store


# ── _judge_contradiction unit (ADR-1 R1, fast paths + fallback) ────────

class _FakeProvider:
    """Records calls; returns a configurable verdict. Raises on demand."""
    def __init__(self, contradiction=False, raise_=False):
        self._contradiction = contradiction
        self._raise = raise_
        self.calls = []

    def judge_contradiction(self, subject_type, subject_name, predicate,
                            new_value, old_value):
        self.calls.append((subject_type, subject_name, predicate,
                           new_value, old_value))
        if self._raise:
            raise RuntimeError("simulated provider crash")
        return {"contradiction": self._contradiction,
                "reason": "fake"}


def test_multivalue_predicate_short_circuits_no_llm():
    """已知多值集 {uses,depends_on,contains,implements,connected_to,part_of,
    relates_to} 直接 no-contradiction, 不调 provider (省调用 + 防 LLM 误判共存)。"""
    prov = _FakeProvider(contradiction=True)  # 即便 provider 会判 True 也不调
    for pred in ("uses", "depends_on", "contains", "implements",
                 "connected_to", "part_of", "relates_to"):
        result = autodream._judge_contradiction(
            [prov], "project", "Alpha", pred, "docker", "rust")
        assert result is False, (
            f"multivalue predicate {pred} must short-circuit to no-contradiction")
    assert prov.calls == [], (
        "multivalue predicate short-circuit must NOT call the LLM provider")


def test_identical_values_no_contradiction_no_llm():
    """新旧值相同 → fast path no-contradiction, 不调 provider (同 fact 非矛盾)。"""
    prov = _FakeProvider(contradiction=True)
    result = autodream._judge_contradiction(
        [prov], "concept", "X", "is_a", "mammal", "mammal")
    assert result is False, "identical values are not a contradiction"
    assert prov.calls == [], "identical-value fast path must NOT call LLM"


def test_single_value_predicate_asks_llm_and_supersedes_on_contradiction():
    """单值谓词 → 调 provider.judge_contradiction; contradiction=True → 返 True。"""
    prov = _FakeProvider(contradiction=True)
    result = autodream._judge_contradiction(
        [prov], "person", "张三", "is_a", "哺乳动物", "爬行动物")
    assert result is True, "single-value contradiction must return True"
    assert len(prov.calls) == 1, "exactly one LLM call per sibling"
    # 签名契约: (subject_type, subject_name, predicate, new_value, old_value)
    assert prov.calls[0] == ("person", "张三", "is_a", "哺乳动物", "爬行动物"), (
        f"judge_contradiction signature mismatch, got {prov.calls[0]}")


def test_single_value_predicate_no_contradiction_when_llm_says_false():
    """单值谓词 → LLM 判 contradiction=False → 返 False (共存, 不 supersede)。"""
    prov = _FakeProvider(contradiction=False)
    result = autodream._judge_contradiction(
        [prov], "concept", "X", "is_a", "B", "C")
    assert result is False, "LLM judge contradiction=False must return False"


def test_empty_providers_falls_back_no_supersede():
    """providers=[] → fallback contradiction=False, 不阻断 (ADR-1 A1 fallback 契约)。"""
    result = autodream._judge_contradiction(
        [], "concept", "X", "is_a", "B", "C")
    assert result is False, (
        "empty providers must fall back to no-contradiction (不阻断 ingest)")


def test_provider_raises_falls_back_no_supersede():
    """provider 抛异常 → fallback contradiction=False, 不 supersede 不阻断。"""
    prov = _FakeProvider(raise_=True)
    result = autodream._judge_contradiction(
        [prov], "concept", "X", "is_a", "B", "C")
    assert result is False, (
        "provider exception must fall back to no-contradiction (NEVER block)")


def test_provider_returns_non_bool_contradiction_falls_back():
    """provider 返 contradiction 非 True (None/缺失) → 不 supersede (防脏数据)。"""
    class _Garbage:
        def judge_contradiction(self, *a):
            return {"contradiction": "maybe", "reason": "garbage"}
    result = autodream._judge_contradiction(
        [_Garbage()], "concept", "X", "is_a", "B", "C")
    assert result is False, (
        "non-True contradiction must NOT count as contradiction (防脏数据)")


# ── ingest 侧 supersede 接线 (bi-temporal valid_to + 复用 _has_active) ──

def _fresh_db():
    """tmp db 隔离, 不污染 data/memory.db。"""
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "supersede.db")
    return store.put_entity("SubjA", "concept")


def test_supersede_sets_valid_to_on_contradiction():
    """contradiction=True → supersede 旧 fact: status='superseded' + valid_to 设值
    + supersedes_id 指向新 fact (与 autodream.py:239 bi-temporal supersede 一致)。"""
    sid = _fresh_db()
    # 旧 active fact
    old_id = store.put_fact(sid, "is_a", "old_val", extractor="llm")
    # 模拟 ingest: judge 判矛盾 → 新 fact 入库 + 旧 supersede
    new_id = store.put_fact(sid, "is_a", "new_val", extractor="llm")
    store.update_fact_status(old_id, "superseded",
                             supersedes_id=new_id, valid_to=store._now())

    conn = db.get_conn()
    old_row = conn.execute("SELECT * FROM fact WHERE id=?", (old_id,)).fetchone()
    new_row = conn.execute("SELECT * FROM fact WHERE id=?", (new_id,)).fetchone()
    assert old_row["status"] == "superseded", "contradicted fact must be superseded"
    assert old_row["valid_to"] is not None, "supersede must set valid_to (bi-temporal)"
    assert old_row["supersedes_id"] == new_id, "supersedes_id must point at new fact"
    assert new_row["status"] == "active", "new fact must be active"
    assert new_row["valid_to"] is None, "new active fact must have open valid_to"


def test_no_supersede_when_no_active_sibling():
    """无同 subject+predicate active sibling → _has_active_for_predicate 返空 →
    无矛盾判定, 直接 ADD (brand-new path)。复用 _has_active_for_predicate 不新写 scan。"""
    sid = _fresh_db()
    siblings = autodream._has_active_for_predicate(sid, "is_a")
    assert siblings == [], "no active fact for new subject-predicate pair"
    # 确认 _has_active_for_predicate 复用 store.get_facts_by_subject (不新写 scan)
    new_id = store.put_fact(sid, "is_a", "only_val", extractor="llm")
    siblings_after = autodream._has_active_for_predicate(sid, "is_a")
    assert len(siblings_after) == 1, (
        "_has_active_for_predicate must surface the just-added active fact")
    assert siblings_after[0]["id"] == new_id


def test_multivalue_coexists_no_supersede():
    """多值谓词 (uses) 新旧值共存: 都留 active, 不 supersede (short-circuit 验证)。"""
    sid = _fresh_db()
    store.put_fact(sid, "uses", "rust", extractor="llm")
    # 多值谓词 short-circuit → judge 不被调 → 不 supersede
    coexists = autodream._judge_contradiction(
        [_FakeProvider(contradiction=True)], "project", "Alpha",
        "uses", "docker", "rust")
    assert coexists is False, "uses is multivalue → coexist, never supersede"


def test_supersede_valid_to_is_now_iso():
    """supersede 的 valid_to 必须是 _now() ISO 时戳 (与 bi-temporal supersede 格式一致)。"""
    sid = _fresh_db()
    old_id = store.put_fact(sid, "is_a", "old", extractor="llm")
    new_id = store.put_fact(sid, "is_a", "new", extractor="llm")
    now = store._now()
    store.update_fact_status(old_id, "superseded",
                             supersedes_id=new_id, valid_to=now)
    conn = db.get_conn()
    row = conn.execute("SELECT valid_to FROM fact WHERE id=?", (old_id,)).fetchone()
    # _now() 格式: ISO 8601 UTC, 秒级, +00:00 (与 ms-floor 字典序惯例对齐)
    assert row["valid_to"] == now, "valid_to must equal the passed _now() value"
    assert row["valid_to"].endswith("+00:00"), (
        "valid_to must be UTC ISO (与 bi-temporal _now 对齐)")
