"""H5 migrate_v2 测试 — tmp fixture 蒸馏产物 + tmp db, 不动生产库/生产 temp/."""
import json
import sqlite3
from pathlib import Path

import db
import migrate_v2 as mig
import store

import pytest


@pytest.fixture(autouse=True)
def _pin_repo_root(monkeypatch):
    """_REPO_ROOT 是 Path.home() 派生的本机白名单根; CI HOME=/home/runner
    下 '/home/yy/projects/...' fixture 不再命中 → repo tag 不铸、断言差元素。
    pin 回本机根: repo_tag 是纯路径运算 (不 touch 文件系统), 路径无需存在,
    本套件的硬编码 cwd/断言跨机成立。"""
    monkeypatch.setattr(mig, "_REPO_ROOT", Path("/home/yy/projects"))


def _fixture(root: Path) -> dict:
    """最小蒸馏产物: 3 atom / 2 edge (1 条 a>b 反序验 a<b 规范化) / 溯源齐备。"""
    t = root / "temp"
    t.mkdir()
    units = [
        {"id": 0, "cluster": 0, "sentences": ["句甲一。", "句甲二。"]},
        {"id": 1, "cluster": 0, "sentences": ["句乙。"]},
        {"id": 2, "cluster": 1, "sentences": ["句丙。"]},
    ]
    (t / "proto_units.json").write_text(
        json.dumps({"units": units}, ensure_ascii=False))
    atoms = [
        {"aid": 0, "text": "atom甲结论。", "members": [0],
         "label": "fact", "p_dur_max": 0.7},
        {"aid": 1, "text": "atom乙裁决。", "members": [1],
         "label": "judgment", "p_dur_max": 0.5},
        {"aid": 2, "text": "atom丙终态。", "members": [2],
         "label": "summary", "p_dur_max": 0.0},
    ]
    (t / "full_e_atoms.json").write_text(json.dumps(atoms, ensure_ascii=False))
    (t / "full_f_edges.jsonl").write_text(
        '{"a": 0, "b": 1, "w": 0.6}\n{"a": 2, "b": 0, "w": 0.4}\n')
    (t / "full_a_summaries.json").write_text(
        json.dumps({"0": "兜底甲。"}, ensure_ascii=False))
    return mig.load_inputs(root)


def _seed_facts(tmp_path, monkeypatch):
    """目标 tmp db: 句甲/句乙可溯源 (session refs + repo cwd), 句丙溯源缺席。"""
    db.init(Path(tmp_path) / "v2.db")
    sid = store.put_entity("甲主题", "topic")
    store.put_fact(sid, "p", "v1", topic="句甲一。",
                   source_refs=["session:memory:a.md#0", "session:memory:b.md#1"],
                   source_cwd="/home/yy/projects/memory-service")
    store.put_fact(sid, "p", "v2", topic="句甲一。",   # 同句双 fact: refs 并集
                   source_refs=["session:memory:a.md#3"],
                   source_cwd="/home/yy/projects/memory-service")
    store.put_fact(sid, "p", "v3", topic="句乙。",
                   source_refs=["session:memory:b.md#0"],
                   source_cwd="/tmp")                   # 白名单外 cwd → 无 repo tag
    store.put_fact(sid, "p", "v4", topic="句丁未入atom。",
                   source_refs=["session:memory:z.md#0"])  # 非 member 句, 不进图
    return db.get_conn()


def test_plan_traces_provenance_and_mints_tags(tmp_path, monkeypatch):
    inp = _fixture(tmp_path)
    conn = _seed_facts(tmp_path, monkeypatch)
    atoms, st = mig.plan(inp, conn)
    a0 = next(a for a in atoms if a["aid"] == 0)
    assert st["atoms"] == 3 and st["edges_loadable"] == 2
    # refs 并集 (#3 与 #0/#1 同 session 去尾合一), cwd 多数决
    assert set(a0["tags"]) == {"session:memory:a.md", "session:memory:b.md",
                               "repo:memory-service"}
    assert json.loads(a0["source_refs"]) == [
        "session:memory:a.md#0", "session:memory:a.md#3", "session:memory:b.md#1"]
    assert a0["source_cwd"] == "/home/yy/projects/memory-service"
    assert a0["valid_from"] is not None
    a1 = next(a for a in atoms if a["aid"] == 1)
    assert set(a1["tags"]) == {"session:memory:b.md"}   # /tmp cwd 不铸 repo tag
    a2 = next(a for a in atoms if a["aid"] == 2)
    assert a2["tags"] == [] and a2["source_refs"] is None  # 溯源缺席不臆测


def test_plan_without_conn_is_pure(tmp_path):
    """dry-run 无库连接: 零溯源仍可计划 (计数在, 字段空)。"""
    atoms, st = mig.plan(_fixture(tmp_path), None)
    assert st["atoms"] == 3 and st["tag_session"] == 0
    assert all(a["source_refs"] is None and a["tags"] == [] for a in atoms)


def test_execute_loads_and_is_idempotent(tmp_path, monkeypatch):
    inp = _fixture(tmp_path)
    conn = _seed_facts(tmp_path, monkeypatch)
    target = Path(tmp_path) / "v2.db"
    atoms, _ = mig.plan(inp, conn)
    r1 = mig.execute(target, inp, atoms)
    assert (r1["atoms"], r1["edges"], r1["tags"]) == (3, 2, 3)
    q = lambda s: conn.execute(s).fetchone()[0]
    assert q("SELECT count(*) FROM atom") == 3
    assert q("SELECT count(*) FROM atom_edge") == 2
    assert q("SELECT count(*) FROM tag") == 3          # a.md + b.md + repo
    assert q("SELECT count(*) FROM tag_mount") == 4    # a0 三 tag + a1 一 tag
    assert q("SELECT count(*) FROM tag WHERE kind='factual' AND level=1") == 3
    # 幂等: 重跑零重复 (atoms 按文本寻回, 边/挂载 OR IGNORE)
    r2 = mig.execute(target, inp, atoms)
    assert r2 == {"atoms": 0, "edges": 0, "tags": 0, "mounts": 0}
    assert q("SELECT count(*) FROM atom") == 3
    assert q("SELECT count(*) FROM atom_edge") == 2
    assert q("SELECT count(*) FROM tag_mount") == 4


def test_edge_a_lt_b_normalized(tmp_path, monkeypatch):
    """jsonl 反序边 (a=2,b=0) 落库规范 a<b; CHECK(a_id<b_id) 由 DB 兜底强制。"""
    inp = _fixture(tmp_path)
    conn = _seed_facts(tmp_path, monkeypatch)
    atoms, _ = mig.plan(inp, conn)
    mig.execute(Path(tmp_path) / "v2.db", inp, atoms)
    rows = conn.execute("SELECT a_id, b_id, w, kind FROM atom_edge").fetchall()
    assert all(r[0] < r[1] for r in rows) and len(rows) == 2
    assert {r[3] for r in rows} == {"related"}
    ids = sorted(r[0] for r in conn.execute("SELECT id FROM atom"))
    import pytest
    with pytest.raises(sqlite3.IntegrityError):        # DB 约束: 反序直插必拒
        conn.execute("INSERT INTO atom_edge VALUES(?,?,?,'related')",
                     (ids[2], ids[1], 0.9))
    with pytest.raises(sqlite3.IntegrityError):        # label CHECK 四型外必拒
        conn.execute("INSERT INTO atom(text, label) VALUES('x','process')")


def test_repo_tag_whitelist():
    assert mig.repo_tag("/home/yy/projects/memory-service") == "repo:memory-service"
    assert mig.repo_tag("/home/yy/projects/dais-hub/sub/dir") == "repo:dais-hub"
    assert mig.repo_tag("/tmp") is None
    assert mig.repo_tag("/home/yy/warpdotdev/dais") is None
    assert mig.repo_tag(None) is None
    assert mig.session_tags(["session:memory:a.md#0", "session:memory:a.md#9",
                             "session:debt2-e2e", "malformed"]) == \
        {"session:memory:a.md", "session:debt2-e2e"}
