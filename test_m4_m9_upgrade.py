"""M4 upgrade_queue + M9 surprise 批验收测试 (spec v2 §1 M4/M4-v2 + §2 M9)。

覆盖派发令五条验收:
1. 建表迁移: 新库含表; 旧库副本(无表)幂等补表; 重复 init 无副作用。
2. 入队幂等: 同 material_ref 二次入队不重复 (M8 segment 点 + M6 fact 点各测)。
3. M9 断言: monkeypatch embed 可控向量 — 重复文本 novelty≈0 / 全新文本高;
   gazetteer miss 实体惊喜; 表外谓词结构惊喜; priority=|surprise|^α 落列;
   离线 embed → novelty/surprise NULL + priority 0 (降级不 crash)。
4. G3 流转: fake 消费者 pending→in_flight(done) / 失败 attempts+1 退回 /
   attempts≥3→dead; 批取 priority 降序。
5. 主径不回归: autodream 全管道(零 LLM/网络)跑通且入队发生; 重跑全 NOOP
   且队列零新增。

测试规范: def test_xxx() 函数让 pytest 收集。禁网络/LLM: embedding.embed
monkeypatch 可控向量。
"""
import json
import sqlite3
import tempfile
import uuid
from pathlib import Path

import autodream
import db
import embedding
import surprise
import upgrade


def _write_transcript(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8")


def _fresh_db(name: str) -> str:
    tmp = tempfile.mkdtemp()
    db.init(Path(tmp) / f"{name}.db")
    return tmp


# 可控向量 fixture: 两个正交基向量, 文本前缀选向量 (禁网络)。
_V_A = [1.0, 0.0]
_V_B = [0.0, 1.0]


def _install_embed_map(mapping: dict):
    """embed(text): 按 '文本→向量' 精确映射; 未命中 → [] (模拟离线)。"""
    orig = embedding.embed
    embedding.embed = lambda text, providers=None: list(
        mapping.get(text, []))
    return orig


# ── 验收 1: 建表迁移 ─────────────────────────────────────────────────

def test_new_db_has_upgrade_queue():
    tmp = _fresh_db("newq")
    tables = {r[0] for r in db.get_conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "upgrade_queue" in tables, tables
    cols = {r[1] for r in db.get_conn().execute("PRAGMA table_info(upgrade_queue)")}
    assert {"id", "material_ref", "transcript_path", "byte_offset", "surprise",
            "priority", "status", "attempts", "created_at", "updated_at"} <= cols


def test_old_db_copy_gets_table_idempotent():
    """旧库副本 (M1/M2/M3 前老结构 fact 表, 无 upgrade_queue) init 补表;
    清缓存强制重走迁移路径, 重复 init 无副作用。"""
    from test_m1_m3_schema import _OLD_FACT_DDL
    tmp = tempfile.mkdtemp()
    old = Path(tmp) / "old.db"
    conn = sqlite3.connect(str(old))
    conn.executescript("""
        CREATE TABLE entity (id TEXT PRIMARY KEY, name TEXT NOT NULL,
            entity_type TEXT NOT NULL, properties TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL);
    """ + _OLD_FACT_DDL)
    conn.commit()
    conn.close()
    db.init(old)
    tables = {r[0] for r in db.get_conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "upgrade_queue" in tables, "老库 init 后应补 upgrade_queue 表"
    db._conn = None
    db._conn_path = None
    db.init(old)  # 重复 init → CREATE IF NOT EXISTS 幂等, 不抛
    n = db.get_conn().execute(
        "SELECT COUNT(*) FROM upgrade_queue").fetchone()[0]
    assert n == 0


# ── 验收 2: 入队幂等 (两个 wire 点) ──────────────────────────────────

def test_enqueue_segment_idempotent():
    _fresh_db("segidem")
    q1 = upgrade.enqueue_segment("/tmp/t.jsonl", 0, "x" * 3000)
    q2 = upgrade.enqueue_segment("/tmp/t.jsonl", 0, "x" * 3000)  # 同 ref
    assert q1 is not None and q2 is None, "同 material_ref 二次入队必须 no-op"
    n = db.get_conn().execute(
        "SELECT COUNT(*) FROM upgrade_queue").fetchone()[0]
    assert n == 1, f"队列应恰 1 行, got {n}"
    row = db.get_conn().execute(
        "SELECT material_ref, transcript_path, byte_offset, status "
        "FROM upgrade_queue").fetchone()
    assert row["material_ref"] == "segment:/tmp/t.jsonl#seg0"
    assert row["transcript_path"] == "/tmp/t.jsonl"
    assert row["byte_offset"] == 0
    assert row["status"] == "pending"


def test_enqueue_fact_idempotent():
    _fresh_db("factidem")
    fid = uuid.uuid4().hex
    q1 = upgrade.enqueue_fact(fid, subject="S", predicate="uses", obj="O")
    q2 = upgrade.enqueue_fact(fid, subject="S", predicate="uses", obj="O")
    assert q1 is not None and q2 is None
    n = db.get_conn().execute(
        "SELECT COUNT(*) FROM upgrade_queue").fetchone()[0]
    assert n == 1
    assert db.get_conn().execute(
        "SELECT material_ref FROM upgrade_queue").fetchone()[0] == f"fact:{fid}"


# ── 验收 3: M9 三轴断言 ──────────────────────────────────────────────

def test_novelty_duplicate_and_novel_text():
    """embed 可控: KG 既有 value 'alpha beta' → V_A; 候选同文本 → cosine 1 →
    novelty≈0; 候选全新 (正交 V_B) → novelty≈1。"""
    tmp = _fresh_db("nov")
    import store
    sid = store.put_entity("S", "concept")
    store.put_fact(sid, "is_a", "alpha beta")

    orig = _install_embed_map({"alpha beta": _V_A,
                               "brand new world": _V_B})
    try:
        dup = surprise.compute("alpha beta")
        new = surprise.compute("brand new world")
    finally:
        embedding.embed = orig
    assert dup["novelty"] is not None and dup["novelty"] < 0.01, dup
    assert new["novelty"] > 0.99, new
    # 主分量传导: 全新文本复合 surprise 高于重复文本。
    assert new["surprise"] > dup["surprise"]


def test_entity_and_structural_surprise():
    """gazetteer miss: 大写词 'Zephyr' 不在词典也未被 regex 覆盖 → miss;
    显式实体在词典 → 覆盖。表外谓词 'made_of' → structural; 表内 'uses' → 否。"""
    tmp = _fresh_db("ent")
    import store
    store.put_entity("Rust 语言", "tool", aliases=["rust"])

    orig = _install_embed_map({})  # 离线 → novelty None, 只看两轴加成
    try:
        miss = surprise.compute("Zephyr is fast", entities=("Zephyr",))
        hit = surprise.compute("rust is fast", entities=("rust",))
        struct = surprise.compute("x", predicates=("made_of",))
        in_table = surprise.compute("x", predicates=("uses",))
    finally:
        embedding.embed = orig

    assert miss["entity_miss"] == 1.0, f"词典 miss 应 1.0, got {miss}"
    assert hit["entity_miss"] == 0.0, f"词典命中应 0.0, got {hit}"
    assert struct["structural"] is True, "表外谓词应标记结构惊喜"
    assert in_table["structural"] is False, "表内谓词不应标记"
    # 离线: novelty None → surprise None, priority 0 (降级不 crash)。
    assert miss["novelty"] is None and miss["surprise"] is None
    assert miss["priority"] == 0.0


def test_priority_formula_alpha():
    """priority = |surprise|^α: α=1 恒等; α=2 平方 (monkeypatch 常量)。"""
    tmp = _fresh_db("alpha")
    import store
    sid = store.put_entity("S", "concept")
    store.put_fact(sid, "is_a", "alpha beta")
    orig = _install_embed_map({"alpha beta": _V_A,
                               "brand new world": _V_B})
    try:
        r1 = surprise.compute("brand new world", entities=("Zephyr",))
        surprise._ALPHA = 2.0
        r2 = surprise.compute("brand new world", entities=("Zephyr",))
    finally:
        embedding.embed = orig
        surprise._ALPHA = 1.0
    assert abs(r1["priority"] - r1["surprise"]) < 1e-9, r1
    assert abs(r2["priority"] - r2["surprise"] ** 2) < 1e-9, r2
    # 入队时算好落列。
    orig2 = _install_embed_map({"S uses O": _V_B})
    try:
        upgrade.enqueue_fact(uuid.uuid4().hex, subject="S",
                             predicate="uses", obj="O")
    finally:
        embedding.embed = orig2
    row = db.get_conn().execute(
        "SELECT surprise, priority FROM upgrade_queue").fetchone()
    assert row["surprise"] is not None and row["priority"] > 0.9
    assert abs(row["priority"] - row["surprise"]) < 1e-9


# ── 验收 4: G3 流转 (fake 消费者) ────────────────────────────────────

def _seed_three_items():
    """三个 pending 项, priority 人工设定 (跳过 M9, 直插可控)。"""
    _fresh_db("g3")
    conn = db.get_conn()
    now = "2026-01-01T00:00:00+00:00"
    for ref, prio in (("low", 0.2), ("high", 0.9), ("mid", 0.5)):
        conn.execute(
            "INSERT INTO upgrade_queue (id, material_ref, surprise, priority, "
            "status, attempts, created_at, updated_at) VALUES (?,?,?,?,'pending',0,?,?)",
            (uuid.uuid4().hex, ref, prio, prio, now, now))
    conn.commit()


def test_dequeue_priority_desc_batch_and_in_flight():
    _seed_three_items()
    batch = upgrade.dequeue()  # 缺省 ≤20
    refs = [r["material_ref"] for r in batch]
    assert refs == ["high", "mid", "low"], f"批取应 priority 降序, got {refs}"
    # 全部置 in_flight; 再 dequeue 不重复取。
    statuses = {r["material_ref"]: r["status"] for r in db.get_conn().execute(
        "SELECT material_ref, status FROM upgrade_queue").fetchall()}
    assert set(statuses.values()) == {"in_flight"}
    assert upgrade.dequeue() == [], "in_flight 项不得重复出队"


def test_dequeue_limit_respected():
    _seed_three_items()
    batch = upgrade.dequeue(limit=2)
    assert len(batch) == 2 and batch[0]["material_ref"] == "high"
    left = db.get_conn().execute(
        "SELECT COUNT(*) FROM upgrade_queue WHERE status='pending'").fetchone()[0]
    assert left == 1


def test_mark_done_flow():
    _seed_three_items()
    item = upgrade.dequeue(limit=1)[0]
    upgrade.mark_done(item["id"])
    row = db.get_conn().execute(
        "SELECT status FROM upgrade_queue WHERE id=?", (item["id"],)).fetchone()
    assert row["status"] == "done"


def test_mark_failed_retry_then_dead():
    """失败 ×2 → attempts 1/2 退回 pending; 第 3 次 → dead 冻结。"""
    _seed_three_items()
    item = upgrade.dequeue(limit=1)[0]
    assert upgrade.mark_failed(item["id"]) == "pending"
    assert upgrade.mark_failed(item["id"]) == "pending"
    row = db.get_conn().execute(
        "SELECT status, attempts FROM upgrade_queue WHERE id=?",
        (item["id"],)).fetchone()
    assert row["status"] == "pending" and row["attempts"] == 2
    assert upgrade.mark_failed(item["id"]) == "dead"
    row = db.get_conn().execute(
        "SELECT status, attempts FROM upgrade_queue WHERE id=?",
        (item["id"],)).fetchone()
    assert row["status"] == "dead" and row["attempts"] == 3
    # dead 不再被批取 (不无限重试, 冻结待人工)。
    refs = [r["material_ref"] for r in upgrade.dequeue()]
    assert item["material_ref"] not in refs


# ── 验收 5: 主径不回归 — 全管道入队发生 + 幂等 ────────────────────────

def test_pipeline_enqueues_and_rerun_noop():
    """fixture: 一条超长 tool_result 段 (>1200 截尾入队) + 一条产出 fact 的
    user 段 (占位 fact 入队); 重跑全 NOOP 且队列零新增。"""
    tmp = _fresh_db("pipe")
    tpath = Path(tmp) / "t.jsonl"
    _write_transcript(tpath, [
        {"type": "user", "message": {"content":
            [{"type": "text", "text": "FastAPI uses Pydantic."}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "obs " * 500}]}},  # 2000 字符 → 截尾
    ])
    orig = embedding.embed
    embedding.embed = lambda text, providers=None: []  # 离线: novelty NULL, priority 0
    try:
        out1 = autodream.autodream("sess-m4", str(tpath))
        out2 = autodream.autodream("sess-m4", str(tpath))
    finally:
        embedding.embed = orig

    assert out1["added"] == 1, out1
    assert out2 == {"added": 0, "updated": 0, "deleted": 0, "noop": 1}, out2
    conn = db.get_conn()
    refs = {r["material_ref"] for r in conn.execute(
        "SELECT material_ref FROM upgrade_queue")}
    assert any(r.startswith(f"segment:{tpath}#seg") for r in refs), (
        f"M8 wire: 截尾段全文应入队, got {refs}")
    fact_refs = [r for r in refs if r.startswith("fact:")]
    assert len(fact_refs) == 1, f"M6 wire: 占位 fact 应入队 1 项, got {fact_refs}"
    # 幂等: 重跑后队列总数不变 (同 material_ref 拒重)。
    n = conn.execute("SELECT COUNT(*) FROM upgrade_queue").fetchone()[0]
    assert n == 2, f"重跑不得新增队列行, got {n}"
