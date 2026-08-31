"""M1/M2/M3 schema 批验收测试 (spec v2 §1: supersede_reason / provenance / veracity)。

覆盖派发令五条验收:
1. 新库 init 后 fact 表含三新列。
2. 旧库副本(无三新列的老表) init 幂等补列; 重复 init 无副作用。
3. put_fact provenance → veracity 五档映射 (user_prose 1.0 / tool_obs 0.9 /
   human 0.9 / agent_assert 0.5 / system 0.5); 显式 veracity 优先; 缺省 provenance
   → 双 NULL (存量/legacy 不可考不回填不臆测)。
4. update_fact_status reason 落库 + COALESCE 不覆盖; 两调用点
   (consolidate dedup / autodream contradiction) 新写入必带 reason。

测试规范: def test_xxx() 函数让 pytest 收集 (本项目头号雷区=模块级裸 assert 死代码,
test_bi_temporal/test_bfs_recall/test_as_of_normalize 是历史债勿复制)。
禁网络/LLM: embedding.embed monkeypatch 为 [], adapter.extract_facts monkeypatch
为固定 Extraction, judge 用 _FakeProvider (循 test_autodream_supersede 先例)。
"""
import json
import sqlite3
import tempfile
from pathlib import Path

import autodream
import gazetteer
import consolidate
import db
import embedding
import store
from llm_provider import EdgeOut, EntityOut, Extraction


# ── 验收 1: 新库 init 后 fact 表含三新列 ─────────────────────────────

def test_new_db_fact_has_three_new_columns():
    """新库 db.init → PRAGMA table_info(fact) 必含 supersede_reason/provenance/veracity
    (schema.sql 新装路径 + db.py 迁移路径双通)。"""
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "new.db")
    cols = {r[1] for r in db.get_conn().execute("PRAGMA table_info(fact)")}
    for col in ("supersede_reason", "provenance", "veracity"):
        assert col in cols, f"新库 fact 表缺列 {col}, got {sorted(cols)}"
    # 类型对齐 spec: reason/provenance TEXT, veracity REAL (DR-6 标量)。
    decl = {r[1]: r[2].upper() for r in db.get_conn().execute("PRAGMA table_info(fact)")}
    assert decl["supersede_reason"] == "TEXT"
    assert decl["provenance"] == "TEXT"
    assert decl["veracity"] == "REAL"


# ── 验收 2: 旧库副本幂等补列 + 重复 init 无副作用 ─────────────────────

# 老版 fact DDL = 现物 schema.sql 减去 M1/M2/M3 三新列 (迁移前实态)。
_OLD_FACT_DDL = """
CREATE TABLE fact (
    id            TEXT PRIMARY KEY,
    subject_id    TEXT NOT NULL,
    predicate     TEXT NOT NULL,
    object_id     TEXT,
    value         TEXT,
    valid_from    TEXT,
    valid_to      TEXT,
    fact_type     TEXT NOT NULL DEFAULT 'stable',
    LIF           REAL NOT NULL DEFAULT 0.5,
    original_lif  REAL NOT NULL DEFAULT 0.5,
    confidence    REAL NOT NULL DEFAULT 0.5,
    source_refs   TEXT NOT NULL DEFAULT '[]',
    extractor     TEXT NOT NULL DEFAULT 'regex',
    status        TEXT NOT NULL DEFAULT 'active',
    supersedes_id TEXT,
    lif_freq        REAL NOT NULL DEFAULT 0,
    lif_recency     REAL NOT NULL DEFAULT 0.5,
    lif_spread      REAL NOT NULL DEFAULT 0,
    lif_coherence   REAL NOT NULL DEFAULT 0,
    lif_source      REAL NOT NULL DEFAULT 0.4,
    access_count    INTEGER NOT NULL DEFAULT 0,
    last_accessed_at TEXT,
    seen_sessions   TEXT NOT NULL DEFAULT '[]',
    source_cwd    TEXT,
    topic         TEXT,
    created_at    TEXT NOT NULL
)
"""


def _make_old_db(path: Path) -> None:
    """模拟迁移前旧库: 老结构 fact 表 + 一行 legacy 数据。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(_OLD_FACT_DDL)
    conn.execute(
        "INSERT INTO fact (id, subject_id, predicate, value, status, created_at) "
        "VALUES ('legacy-1', 'legacy-subj', 'is_a', 'legacy-val', 'active', "
        "'2020-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()


def test_old_db_copy_migrates_columns_no_backfill():
    """旧库副本 init → 幂等补三列; legacy 行保留且三新列全 NULL
    (存量不回填 — writer 不可考不臆测, NULL=legacy)。"""
    tmp = tempfile.mkdtemp()
    old_path = Path(tmp) / "old.db"
    _make_old_db(old_path)
    db.init(old_path)
    cols = {r[1] for r in db.get_conn().execute("PRAGMA table_info(fact)")}
    assert {"supersede_reason", "provenance", "veracity"} <= cols, (
        f"老库迁移后仍缺列, got {sorted(cols)}")
    row = db.get_conn().execute(
        "SELECT value, status, supersede_reason, provenance, veracity "
        "FROM fact WHERE id='legacy-1'"
    ).fetchone()
    assert row["value"] == "legacy-val", "迁移不得动 legacy 行内容"
    assert row["status"] == "active"
    assert row["supersede_reason"] is None, "legacy 行 supersede_reason 必须保持 NULL(不回填)"
    assert row["provenance"] is None, "legacy 行 provenance 必须保持 NULL(不回填)"
    assert row["veracity"] is None, "legacy 行 veracity 必须保持 NULL(不回填)"


def test_repeated_init_no_side_effects():
    """重复 init(强制重跑迁移路径, 模拟新进程同库再 init) → 无异常、无重复列、数据不变。"""
    tmp = tempfile.mkdtemp()
    old_path = Path(tmp) / "old.db"
    _make_old_db(old_path)
    db.init(old_path)
    # 清缓存强制 init 重走 PRAGMA table_info + ALTER 全路径(同进程模拟二次启动;
    # 直接重复调同路径会命中连接缓存, 测不到迁移幂等)。
    db._conn = None
    db._conn_path = None
    db.init(old_path)  # 若 ALTER 未被列检测挡住, 此处 duplicate column name 抛错
    cols = [r[1] for r in db.get_conn().execute("PRAGMA table_info(fact)")]
    assert cols.count("supersede_reason") == 1, "重复 init 不得重复补列"
    assert cols.count("provenance") == 1
    assert cols.count("veracity") == 1
    n = db.get_conn().execute("SELECT COUNT(*) FROM fact").fetchone()[0]
    assert n == 1, f"重复 init 不得动行数, got {n}"


# ── 验收 3: put_fact provenance → veracity 五档映射 ───────────────────

def test_put_fact_provenance_veracity_five_tier_mapping():
    """五档映射断言: user_prose 1.0 / tool_obs 0.9 / human 0.9 / agent_assert 0.5 /
    system 0.5 (P21 出处权重, DR-6 已裁决)。provenance 原样落列。"""
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "map.db")
    sid = store.put_entity("MapSubj", "concept")
    expect = {
        "user_prose": 1.0,
        "tool_obs": 0.9,
        "human": 0.9,
        "agent_assert": 0.5,
        "system": 0.5,
    }
    for prov, want in expect.items():
        fid = store.put_fact(sid, "is_a", f"val-{prov}", provenance=prov)
        row = db.get_conn().execute(
            "SELECT provenance, veracity FROM fact WHERE id=?", (fid,)
        ).fetchone()
        assert row["provenance"] == prov, f"provenance 未落列: {row['provenance']}"
        assert row["veracity"] == want, (
            f"{prov} → veracity 应为 {want}, got {row['veracity']}")
    # 映射表与断言档位一致(防两处漂移)。
    assert store.PROVENANCE_VERACITY == expect


def test_put_fact_veracity_explicit_overrides_mapping():
    """显式传 veracity → 直接采用, 不被 provenance 映射覆盖。"""
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "override.db")
    sid = store.put_entity("OvSubj", "concept")
    fid = store.put_fact(sid, "is_a", "v", provenance="agent_assert", veracity=0.8)
    row = db.get_conn().execute(
        "SELECT veracity FROM fact WHERE id=?", (fid,)).fetchone()
    assert row["veracity"] == 0.8, f"显式 veracity 应优先, got {row['veracity']}"


def test_put_fact_without_provenance_leaves_null():
    """缺省 provenance → provenance/veracity 双 NULL(不可考不臆测, 与存量 legacy 档
    一致); 表外 provenance 值 → veracity 仍 NULL(不猜)。"""
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "null.db")
    sid = store.put_entity("NullSubj", "concept")
    fid = store.put_fact(sid, "is_a", "v")
    row = db.get_conn().execute(
        "SELECT provenance, veracity FROM fact WHERE id=?", (fid,)).fetchone()
    assert row["provenance"] is None and row["veracity"] is None, (
        f"无 provenance 应双 NULL, got {row['provenance']}/{row['veracity']}")
    fid2 = store.put_fact(sid, "is_a", "v2", provenance="garbage_channel")
    row2 = db.get_conn().execute(
        "SELECT veracity FROM fact WHERE id=?", (fid2,)).fetchone()
    assert row2["veracity"] is None, "表外 provenance 不得臆测 veracity"


def test_put_fact_roundtrip_via_get_fact():
    """三新字段经 _decode_fact 全解码回读(store.get_fact 出口一致性)。"""
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "decode.db")
    sid = store.put_entity("DecSubj", "concept")
    fid = store.put_fact(sid, "is_a", "v", provenance="tool_obs")
    f = store.get_fact(fid)
    assert f["provenance"] == "tool_obs"
    assert f["veracity"] == 0.9


def test_task_outcome_roundtrip_and_null_default():
    """prompt v5 任务分诊轴 (2026-08-28): task_outcome 落列 + _decode_fact
    回读; 缺省 None (非任务事实/legacy); 表外值收拢在 llm_extract 层 (store
    不设门 — 单源校验在 TASK_OUTCOMES)。"""
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "outcome.db")
    sid = store.put_entity("OutcomeSubj", "concept")
    cols = {r[1] for r in db.get_conn().execute("PRAGMA table_info(fact)")}
    assert "task_outcome" in cols, "迁移未补 task_outcome 列"
    fid = store.put_fact(sid, "decided", "v", task_outcome="success",
                         raw_predicate="decided-raw")
    f = store.get_fact(fid)
    assert f["task_outcome"] == "success"
    # v1.7 回补收尾: raw_predicate 出口键写读往返 (row.keys() 守卫)。
    assert f["raw_predicate"] == "decided-raw"
    fid2 = store.put_fact(sid, "is_a", "plain v")
    assert store.get_fact(fid2)["task_outcome"] is None
    assert f["supersede_reason"] is None


# ── 验收 4: update_fact_status reason 落库 + 两调用点 ─────────────────

def _fresh_db(tmpdir: str | None = None) -> str:
    """tmp db 隔离 + 预置 subject, 返回 entity id。"""
    tmp = tmpdir or tempfile.mkdtemp()
    db.init(Path(tmp) / "reason.db")
    return store.put_entity("ReasonSubj", "concept")


def test_update_fact_status_reason_persisted():
    """reason 参落库 supersede_reason 列。"""
    sid = _fresh_db()
    old_id = store.put_fact(sid, "is_a", "old")
    new_id = store.put_fact(sid, "is_a", "new")
    store.update_fact_status(old_id, "superseded", supersedes_id=new_id,
                             valid_to=store._now(), reason="dedup")
    row = db.get_conn().execute(
        "SELECT status, supersede_reason FROM fact WHERE id=?", (old_id,)).fetchone()
    assert row["status"] == "superseded"
    assert row["supersede_reason"] == "dedup", (
        f"reason 应落 supersede_reason, got {row['supersede_reason']}")


def test_update_fact_status_reason_coalesce_keeps_existing():
    """不传 reason 的后续调用不得清掉已设 reason (COALESCE 语义, 与 valid_to 先例一致)。"""
    sid = _fresh_db()
    fid = store.put_fact(sid, "is_a", "v")
    store.update_fact_status(fid, "superseded", reason="contradiction")
    store.update_fact_status(fid, "superseded", valid_to=store._now())  # 无 reason
    row = db.get_conn().execute(
        "SELECT supersede_reason FROM fact WHERE id=?", (fid,)).fetchone()
    assert row["supersede_reason"] == "contradiction", "无 reason 调用不得覆盖已设值"


def test_update_fact_status_legacy_call_untouched():
    """老签名三参调用(既有调用点/测试兼容)照常工作, 不写 reason。"""
    sid = _fresh_db()
    fid = store.put_fact(sid, "is_a", "v")
    store.update_fact_status(fid, "deprecated", valid_to=store._now())
    row = db.get_conn().execute(
        "SELECT status, supersede_reason FROM fact WHERE id=?", (fid,)).fetchone()
    assert row["status"] == "deprecated"
    assert row["supersede_reason"] is None


def test_consolidate_dedup_writes_reason():
    """调用点 1: consolidate.py _merge_group dedup supersede 必带 reason='dedup'
    (真跑 decay+dedup 全链, 零 LLM)。"""
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "dedup.db")
    sid = store.put_entity("DedupSubj", "concept")
    dup1 = store.put_fact(sid, "is_a", "same_val", extractor="llm")
    dup2 = store.put_fact(sid, "is_a", "same_val", extractor="llm")
    out = consolidate.consolidate()
    assert out["superseded"] >= 1, f"应有 dedup supersede, got {out}"
    rows = db.get_conn().execute(
        "SELECT id, status, supersede_reason FROM fact "
        "WHERE subject_id=? ORDER BY created_at", (sid,)).fetchall()
    by_id = {r["id"]: r for r in rows}
    survivor, dup = (dup1, dup2) if by_id[dup1]["status"] == "active" else (dup2, dup1)
    assert by_id[survivor]["status"] == "active", "survivor 保持 active"
    assert by_id[dup]["status"] == "superseded"
    assert by_id[dup]["supersede_reason"] == "dedup", (
        f"dedup 调用点必须写 reason, got {by_id[dup]['supersede_reason']}")
    assert by_id[survivor]["supersede_reason"] is None, "survivor 不该有 supersede_reason"


def test_autodream_contradiction_writes_reason():
    """调用点 2: autodream contradiction supersede 必带 reason='contradiction'
    (全管道: monkeypatch adapter.extract_facts + judge 判矛盾, 禁网络/LLM)。"""
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "contra.db")
    sid = store.put_entity("ContraSubj", "concept")
    old_id = store.put_fact(sid, "is_a", "old_val", extractor="llm")

    tpath = Path(tmp) / "session.jsonl"
    tpath.write_text(json.dumps({
        "type": "user",
        "message": {"content": [{"type": "text", "text": "ContraSubj is_a new_val"}]},
    }) + "\n", encoding="utf-8")

    class _Prov:
        """只实现 judge_contradiction: 单值谓词判矛盾 (resolver dedupe_entity 缺失
        → step2 try/except 安全降级, 无网络)。"""
        def judge_contradiction(self, subject_type, subject_name, predicate,
                                new_value, old_value):
            return {"contradiction": True, "reason": "单值谓词不同值"}

    def _fake_extract(text):
        return Extraction(
            entities=[EntityOut("ContraSubj", "concept")],
            edges=[EdgeOut("ContraSubj", "is_a", "new_val", topic="")],
            confidence=0.9,
            source_meta={"provider": "fake", "extractor_label": "llm"},
        )

    orig_embed = embedding.embed
    orig_extract = gazetteer.extract
    embedding.embed = lambda text, providers=None: []  # resolver step2 跳过, 不触本地服务
    gazetteer.extract = _fake_extract  # M6: 提取主径 seam 迁至 gazetteer
    try:
        out = autodream.autodream("sess-m1m3", str(tpath), providers=[_Prov()])
    finally:
        embedding.embed = orig_embed
        gazetteer.extract = orig_extract

    assert out["deleted"] == 1 and out["added"] == 1, (
        f"contradiction 分支应 1 supersede + 1 add, got {out}")
    row = db.get_conn().execute(
        "SELECT status, supersede_reason, supersedes_id FROM fact WHERE id=?",
        (old_id,)).fetchone()
    assert row["status"] == "superseded"
    assert row["supersede_reason"] == "contradiction", (
        f"contradiction 调用点必须写 reason, got {row['supersede_reason']}")
    assert row["supersedes_id"] is not None
    # 新 fact 是 active 且不带 supersede_reason(它是取代者, 不是被取代者)。
    new_row = db.get_conn().execute(
        "SELECT status, supersede_reason FROM fact WHERE id=? AND status='active'",
        (row["supersedes_id"],)).fetchone()
    assert new_row is not None and new_row["supersede_reason"] is None


# ── v1.7 Lane-0: ③④⑤ 共享接缝三列 (extract_sessions/recall_sessions/gate_score) ──

_LANE0_COLS = ("extract_sessions", "recall_sessions", "gate_score")


def test_new_db_fact_has_lane0_three_columns():
    """新库 db.init → PRAGMA table_info(fact) 必含三新列 (schema.sql 新装路径 +
    db.py 迁移路径双通); decl 类型 sessions TEXT / gate_score REAL; 声明缺省
    '[]'/'[]'/0.0。"""
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "lane0-new.db")
    info = list(db.get_conn().execute("PRAGMA table_info(fact)"))
    cols = {r[1] for r in info}
    for col in _LANE0_COLS:
        assert col in cols, f"新库 fact 表缺列 {col}, got {sorted(cols)}"
    decl = {r[1]: r[2].upper() for r in info}
    assert decl["extract_sessions"] == "TEXT"
    assert decl["recall_sessions"] == "TEXT"
    assert decl["gate_score"] == "REAL"
    dflt = {r[1]: (r[4] or "") for r in info}
    assert dflt["extract_sessions"].strip("'") == "[]", (
        f"extract_sessions 声明缺省应为 '[]', got {dflt['extract_sessions']!r}")
    assert dflt["recall_sessions"].strip("'") == "[]", (
        f"recall_sessions 声明缺省应为 '[]', got {dflt['recall_sessions']!r}")
    assert float(dflt["gate_score"]) == 0.0, (
        f"gate_score 声明缺省应为 0.0, got {dflt['gate_score']!r}")


def test_old_db_copy_migrates_lane0_columns_defaults_semantics():
    """旧库副本 init → 幂等补三列; legacy 行内容不动, 三新列呈缺省语义
    (sessions '[]' / gate_score 0.0 — ALTER 声明缺省, 不显式回填)。"""
    tmp = tempfile.mkdtemp()
    old_path = Path(tmp) / "old-lane0.db"
    _make_old_db(old_path)
    db.init(old_path)
    cols = {r[1] for r in db.get_conn().execute("PRAGMA table_info(fact)")}
    assert set(_LANE0_COLS) <= cols, f"老库迁移后仍缺列, got {sorted(cols)}"
    row = db.get_conn().execute(
        "SELECT value, status, extract_sessions, recall_sessions, gate_score "
        "FROM fact WHERE id='legacy-1'").fetchone()
    assert row["value"] == "legacy-val", "迁移不得动 legacy 行内容"
    assert row["status"] == "active"
    assert json.loads(row["extract_sessions"]) == [], (
        f"legacy 行 extract_sessions 应为缺省 '[]', got {row['extract_sessions']!r}")
    assert json.loads(row["recall_sessions"]) == [], (
        f"legacy 行 recall_sessions 应为缺省 '[]', got {row['recall_sessions']!r}")
    assert row["gate_score"] == 0.0, (
        f"legacy 行 gate_score 应为缺省 0.0, got {row['gate_score']!r}")
    # 解码出口一致: _decode_fact 对 legacy 行回落空集/0.0。
    f = store.get_fact("legacy-1")
    assert f["extract_sessions"] == [] and f["recall_sessions"] == []
    assert f["gate_score"] == 0.0


def test_repeated_init_no_side_effects_lane0():
    """重复 init(强制重跑迁移路径) → 无异常、三新列不重复补、数据不变。"""
    tmp = tempfile.mkdtemp()
    old_path = Path(tmp) / "old-lane0-idem.db"
    _make_old_db(old_path)
    db.init(old_path)
    db._conn = None
    db._conn_path = None
    db.init(old_path)  # 若 ALTER 未被列检测挡住, 此处 duplicate column name 抛错
    cols = [r[1] for r in db.get_conn().execute("PRAGMA table_info(fact)")]
    for col in _LANE0_COLS:
        assert cols.count(col) == 1, f"重复 init 不得重复补列 {col}"
    n = db.get_conn().execute("SELECT COUNT(*) FROM fact").fetchone()[0]
    assert n == 1, f"重复 init 不得动行数, got {n}"


def test_put_fact_lane0_roundtrip_and_defaults():
    """写读契约: put_fact 三新参落列 + _decode_fact JSON list 往返; 显式覆盖
    优先; 缺省不臆测 ('[]'/'[]'/0.0); 坏 JSON/非 list 解码回落 []。"""
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "lane0.db")
    sid = store.put_entity("Lane0Subj", "concept")
    fid = store.put_fact(
        sid, "is_a", "v",
        extract_sessions=["s-main-1", "sess-中文"],
        recall_sessions=["s-obs-1"],
        gate_score=1.25,
    )
    # 原始列确为 JSON 文本 (非 list 对象), 含非 ASCII session 原样往返。
    raw = db.get_conn().execute(
        "SELECT extract_sessions, recall_sessions, gate_score FROM fact WHERE id=?",
        (fid,)).fetchone()
    assert json.loads(raw["extract_sessions"]) == ["s-main-1", "sess-中文"]
    assert json.loads(raw["recall_sessions"]) == ["s-obs-1"]
    assert raw["gate_score"] == 1.25
    # _decode_fact 回读: JSON 文本 → list。
    f = store.get_fact(fid)
    assert f["extract_sessions"] == ["s-main-1", "sess-中文"]
    assert f["recall_sessions"] == ["s-obs-1"]
    assert f["gate_score"] == 1.25
    # 缺省: 不传 → '[]'/'[]'/0.0 (不臆测会话/分值)。
    fid2 = store.put_fact(sid, "is_a", "v2")
    f2 = store.get_fact(fid2)
    assert f2["extract_sessions"] == [] and f2["recall_sessions"] == []
    assert f2["gate_score"] == 0.0
    # 解码失败回落 []: 坏 JSON 直接落列 (脏数据防线)。
    db.get_conn().execute(
        "UPDATE fact SET extract_sessions='{bad json' WHERE id=?", (fid,))
    assert store.get_fact(fid)["extract_sessions"] == []
    # 非 list JSON (对象/标量) 同回落 [] (契约类型是 list)。
    db.get_conn().execute(
        'UPDATE fact SET recall_sessions=\'{"not":"a list"}\' WHERE id=?', (fid,))
    assert store.get_fact(fid)["recall_sessions"] == []
    db.get_conn().execute(
        "UPDATE fact SET recall_sessions='42' WHERE id=?", (fid,))
    assert store.get_fact(fid)["recall_sessions"] == []


# v1.7 回补扩面: 双源守卫对象 = 全部 ALTER 系五列 (db.py 逐列 PRAGMA 检测补列者)。
_ALTER_LINEAGE_COLS = (
    "raw_predicate", "task_outcome",
    "extract_sessions", "recall_sessions", "gate_score",
)


def test_alter_lineage_columns_dual_source_guard():
    """双源同步守卫 (红线, v1.7 回补扩面): ALTER 系五列必须 schema.sql 文本 与
    PRAGMA table_info(fact) 两源同在 — 防「只在 db.py ALTER、schema.sql 缺失」
    的不同步先例 (raw_predicate/task_outcome 本体) 重演。任一列单源缺席即红;
    回补两列 decl 类型 TEXT 与 db.py ALTER 现状逐字对齐。"""
    schema_text = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    for col in _ALTER_LINEAGE_COLS:
        assert col in schema_text, f"schema.sql 缺 {col} 列声明 (双源不同步)"
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / "guard.db")
    info = list(db.get_conn().execute("PRAGMA table_info(fact)"))
    cols = {r[1] for r in info}
    for col in _ALTER_LINEAGE_COLS:
        assert col in cols, f"PRAGMA table_info(fact) 缺 {col} (迁移路径缺列)"
    decl = {r[1]: r[2].upper() for r in info}
    assert decl["raw_predicate"] == "TEXT"
    assert decl["task_outcome"] == "TEXT"
