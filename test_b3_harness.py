"""B3 (TICKET B3C-HYG): fact.harness 来源审计列 — schema/迁移/全写入面/stats。

覆盖:
1. 建库: schema.sql 声明 harness TEXT (可空; NULL=legacy/未知)。
2. 老库迁移: pre-harness 旧库 init → ALTER 补列; 幂等 (二次启动不炸不重复);
   legacy 行 harness 保持 NULL (不回填, writer 不可考不臆测)。
3. put_fact 写入 + _decode_fact 出口键 (row-key 守卫兼容未迁移 db)。
4. 写入面 stamp: autodream 管道 (_decide_segments 单点) / bootstrap.init_memory /
   bootstrap.re_ingest_file / cli.mem_write (传参 + MEM_HARNESS env 回落 + NULL) /
   dream wings 升级继承旧 fact harness。
5. stats/stats-json 的 by_harness 分组 (NULL → ``unknown`` 键)。

测试规范: def test_xxx() 函数让 pytest 收集 (本项目头号雷区=模块级裸 assert 死代码)。
"""
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import cli
import db
import store


# ── helpers ──────────────────────────────────────────────────────────

def _mk_db(tmp_path, name="b3.db"):
    """隔离 db (每测试独立文件) → 返回路径。"""
    p = tmp_path / name
    db.init(p)
    return p


_OLD_DDL = """
CREATE TABLE entity (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, entity_type TEXT NOT NULL,
    properties TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE TABLE fact (
    id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, predicate TEXT NOT NULL,
    object_id TEXT, value TEXT, valid_from TEXT, valid_to TEXT,
    fact_type TEXT NOT NULL DEFAULT 'stable',
    LIF REAL NOT NULL DEFAULT 0.5, original_lif REAL NOT NULL DEFAULT 0.5,
    confidence REAL NOT NULL DEFAULT 0.5, source_refs TEXT NOT NULL DEFAULT '[]',
    extractor TEXT NOT NULL DEFAULT 'regex', status TEXT NOT NULL DEFAULT 'active',
    supersedes_id TEXT,
    lif_freq REAL NOT NULL DEFAULT 0, lif_recency REAL NOT NULL DEFAULT 0.5,
    lif_spread REAL NOT NULL DEFAULT 0, lif_coherence REAL NOT NULL DEFAULT 0,
    lif_source REAL NOT NULL DEFAULT 0.4,
    access_count INTEGER NOT NULL DEFAULT 0, last_accessed_at TEXT,
    seen_sessions TEXT NOT NULL DEFAULT '[]', source_cwd TEXT, topic TEXT,
    created_at TEXT NOT NULL
);
"""


def _make_pre_harness_db(path):
    """模拟迁移前旧库: 无 harness 列的 fact 表 + 一行 legacy 数据。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(_OLD_DDL)
    conn.execute(
        "INSERT INTO fact (id, subject_id, predicate, value, status, created_at) "
        "VALUES ('legacy-1', 'legacy-subj', 'is_a', 'legacy-val', 'active', "
        "'2020-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()


def _harness_cols(conn):
    return [r[1] for r in conn.execute("PRAGMA table_info(fact)") if r[1] == "harness"]


def _all_harness_map():
    conn = db.get_conn()
    return {r["id"]: r["harness"]
            for r in conn.execute("SELECT id, harness FROM fact").fetchall()}


# ── schema + migration ───────────────────────────────────────────────

def test_fresh_schema_declares_harness_nullable(tmp_path):
    """新库: fact.harness 声明存在, TEXT 可空 (NULL=legacy/未知允许)。"""
    _mk_db(tmp_path)
    conn = db.get_conn()
    assert _harness_cols(conn) == ["harness"], "harness 列应恰出现一次"
    info = [r for r in conn.execute("PRAGMA table_info(fact)") if r[1] == "harness"][0]
    assert info[2] == "TEXT", f"harness 应为 TEXT, got {info[2]}"
    assert info[3] == 0, "harness 必须可空 (NULL=legacy/未知)"


def test_old_db_migration_idempotent_two_boots(tmp_path):
    """老库迁移: init 补 harness 列一次; 二次启动 (连接重置后重跑 init) 不炸
    不重复; legacy 行 harness 保持 NULL 不回填。"""
    old_path = tmp_path / "old.db"
    _make_pre_harness_db(old_path)
    conn = db.init(old_path)
    assert _harness_cols(conn) == ["harness"], "首次 init 应补 harness 列恰一次"
    row = conn.execute("SELECT harness FROM fact WHERE id='legacy-1'").fetchone()
    assert row["harness"] is None, "legacy 行不得回填 harness"
    # 二次启动: 模拟进程重启 (连接重置) → init 重跑迁移路径仍幂等
    db._conn = None
    db._conn_path = None
    conn2 = db.init(old_path)
    assert _harness_cols(conn2) == ["harness"], "二次 init 不得重复加列"
    row2 = conn2.execute("SELECT harness FROM fact WHERE id='legacy-1'").fetchone()
    assert row2["harness"] is None, "二次启动仍不得回填 legacy 行"


# ── put_fact / decode ────────────────────────────────────────────────

def test_put_fact_harness_roundtrip_and_default_null(tmp_path):
    _mk_db(tmp_path)
    from resolver import resolve_entity
    s = resolve_entity("B3实体甲", "concept", providers=[])
    fid = store.put_fact(s, "uses", "rust", harness="dsh")
    assert store.get_fact(fid)["harness"] == "dsh"
    fid2 = store.put_fact(s, "relates_to", "x")  # 缺省 → NULL=未知
    assert store.get_fact(fid2)["harness"] is None


# ── 写入面 stamp ─────────────────────────────────────────────────────

def _write_user_transcript(p, text):
    p.write_text(json.dumps({"type": "user", "message": {"content": text}},
                            ensure_ascii=False) + "\n", encoding="utf-8")


def test_autodream_stamps_harness(tmp_path):
    """autodream 管道 (ingest-recent 同径): 新增 fact 全带调用 harness。"""
    _mk_db(tmp_path)
    import autodream as autodream_mod
    t = tmp_path / "sess.jsonl"
    _write_user_transcript(t, "用户使用 rust 与 python")
    r = autodream_mod.autodream("sess-b3", str(t), harness="pi")
    assert r["added"] >= 1, f"regex 通道应产出 fact, got {r}"
    got = _all_harness_map()
    assert got and set(got.values()) == {"pi"}, f"全部新增 fact 应 stamp pi, got {got}"


def test_autodream_default_harness_cc(tmp_path):
    """缺省 harness=cc (与既有 corpus_prep 清洗键缺省一致) → fact.harness=cc。"""
    _mk_db(tmp_path)
    import autodream as autodream_mod
    t = tmp_path / "sess2.jsonl"
    _write_user_transcript(t, "用户使用 rust")
    autodream_mod.autodream("sess-b3-cc", str(t))
    got = _all_harness_map()
    assert got and set(got.values()) == {"cc"}, f"缺省应 stamp cc, got {got}"


def test_init_memory_stamps_harness(tmp_path):
    _mk_db(tmp_path)
    import bootstrap
    d = tmp_path / "memdir"
    d.mkdir()
    (d / "native.md").write_text("用户使用 rust", encoding="utf-8")
    (d / "mem-x.md").write_text("---\nsource: mem-service\n---\n用户 uses rust",
                                encoding="utf-8")  # ADR-16f 投影产物被跳过
    r = bootstrap.init_memory(d, harness="dsh")
    assert r["files"] == 1 and r["added"] >= 1, f"init_memory 应产出 fact, got {r}"
    got = _all_harness_map()
    assert got and set(got.values()) == {"dsh"}, f"全部 fact 应 stamp dsh, got {got}"


def test_re_ingest_file_stamps_harness(tmp_path):
    _mk_db(tmp_path)
    import bootstrap
    md = tmp_path / "note.md"
    md.write_text("用户使用 rust", encoding="utf-8")
    r = bootstrap.re_ingest_file(md, harness="omp")
    assert r["added"] >= 1, f"re_ingest_file 应产出 fact, got {r}"
    got = _all_harness_map()
    assert got and set(got.values()) == {"omp"}, f"全部 fact 应 stamp omp, got {got}"


def test_mem_write_harness_param_env_then_null(tmp_path, monkeypatch):
    """write 通道: 显式传参 > MEM_HARNESS env 回落 > NULL (未知允许)。"""
    _mk_db(tmp_path)
    monkeypatch.delenv("MEM_HARNESS", raising=False)
    r1 = cli.mem_write("B3主体", "uses", "rust", channel="agent", harness="pi")
    assert store.get_fact(r1["written"])["harness"] == "pi"
    r2 = cli.mem_write("B3主体", "relates_to", "y", channel="agent")
    assert store.get_fact(r2["written"])["harness"] is None
    monkeypatch.setenv("MEM_HARNESS", "omp")
    r3 = cli.mem_write("B3主体", "uses", "tool", channel="agent")
    assert store.get_fact(r3["written"])["harness"] == "omp"


def test_wings_upgrade_inherits_old_harness(tmp_path):
    """dream wings 升级 (fact: 素材): 新 fact 继承被升级旧 fact 的 harness。"""
    _mk_db(tmp_path)
    import dream
    from resolver import resolve_entity
    s = resolve_entity("B3升级体", "concept", providers=[])
    old = store.put_fact(s, "uses", "regex", harness="dsh")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    new = dream._put_wings_fact(
        subject_id=s, predicate="uses", value="llm", object_id=None,
        provenance="system", extractor="llm", src_refs=[], sessions=[],
        now=now, harness=store.get_fact(old)["harness"])
    assert store.get_fact(new)["harness"] == "dsh", "升级产物应继承旧 fact harness"
    # 缺省 (无上下文) → NULL
    new2 = dream._put_wings_fact(
        subject_id=s, predicate="uses", value="vote", object_id=None,
        provenance="system", extractor="vote", src_refs=[], sessions=[],
        now=now)
    assert store.get_fact(new2)["harness"] is None


# ── stats 分组 ───────────────────────────────────────────────────────

def test_stats_by_harness_grouping(tmp_path):
    """stats by_harness: 按 harness 计数分组; NULL 归 unknown 键。"""
    _mk_db(tmp_path)
    from resolver import resolve_entity
    s = resolve_entity("B3统计体", "concept", providers=[])
    store.put_fact(s, "uses", "a", harness="cc")
    store.put_fact(s, "uses", "b", harness="cc")
    store.put_fact(s, "uses", "c", harness="pi")
    store.put_fact(s, "uses", "d")  # legacy/未知
    by = cli.stats()["by_harness"]
    assert by == {"cc": 2, "pi": 1, "unknown": 1}, f"分组口径不符, got {by}"
    # 分组总和 = fact 总数 (同口径)
    assert sum(by.values()) == cli.stats()["facts"]
