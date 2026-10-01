"""H4 tag_dream 测试: mock embedding/zhipu (零网络/零生产库), tmp db 隔离。

覆盖: 聚类+挂载 / 小簇丢弃 / 撞名后缀 / 幂等重跑(成员重叠复用) /
层级第二层涌现+parent_of / 新 atom 挂载(阈值+needs_embed 候选) /
空库零调用 / dry-run 零 LLM。
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 仓根平铺模块

import db  # noqa: E402
import embedding  # noqa: E402
import src.tag_dream as td  # noqa: E402

DIM = 128  # > _EMBED_DIM_MIN(100) 才过坏向量判据


def _oh(i):
    v = [0.0] * DIM
    v[i] = 1.0
    return v


def _bridge(cos):
    """与 _oh(0) 的 cos=cos, 与 _oh(1) 的 cos=sqrt(1-cos²) 的单位向量。"""
    s = (1.0 - cos * cos) ** 0.5
    return [cos, s] + [0.0] * (DIM - 2)


class FakeZhipu:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, system, messages, max_tokens=1500, tools=None, tool_choice=None):
        self.calls.append(messages)
        assert self.responses, "意外的多余 zhipu 调用"
        return self.responses.pop(0)


def _fake_embed(monkeypatch, mapping):
    def batch(texts):
        out = []
        for t in texts:
            hit = next((v for mk, v in mapping.items() if mk in t), None)
            out.append(list(hit) if hit is not None else _oh(11))
        return out
    monkeypatch.setattr(embedding, "embed_batch", batch)


def _zhipu(monkeypatch, responses):
    fake = FakeZhipu(responses)
    monkeypatch.setattr(td, "_get_zhipu", lambda: fake)
    return fake


def _arr(items):
    return json.dumps(items, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "t.db")
    db.init(p)
    return p


def _add_atoms(conn, texts, needs_embed=0):
    for t in texts:
        conn.execute("INSERT INTO atom(text, label, needs_embed) VALUES(?, 'fact', ?)",
                     (t, needs_embed))


def _names(conn):
    return sorted(r[0] for r in conn.execute("SELECT name FROM tag"))


# ── L1 聚类 + 挂载 + 小簇丢弃 ──────────────────────────────
def test_mint_clusters_mounts_small_dropped(db_path, monkeypatch):
    conn = db.get_conn()
    _add_atoms(conn, ["甲组句%d #c0" % i for i in range(5)]
               + ["乙组句%d #c1" % i for i in range(5)] + ["散句 #c2", "散句 #c2"])
    _fake_embed(monkeypatch, {"#c0": _oh(0), "#c1": _oh(1), "#c2": _oh(2)})
    fake = _zhipu(monkeypatch, [_arr([
        {"id": 0, "name": "标签甲", "description": "甲主题 #d0"},
        {"id": 1, "name": "标签乙", "description": "乙主题 #d1"},
    ])])
    r = td.mint_semantic_tags(db_path)
    assert (r["clusters_kept"], r["clusters_dropped_small"]) == (2, 1)
    assert r["sizes"] == [5, 5]
    assert set(_names(conn)) == {"标签甲", "标签乙"}
    assert r["orphan_atoms"] == 2
    assert r["levels"] == {"1": {"tags": 2, "mounts": 10}}
    mounts = conn.execute("SELECT w FROM tag_mount").fetchall()
    assert len(mounts) == 10 and all(m[0] == 1.0 for m in mounts)
    assert conn.execute("SELECT parent_id FROM tag WHERE parent_id IS NOT NULL"
                        ).fetchone() is None  # 无跨簇边 → 无 L2
    assert len(fake.calls) == 1  # 单批命名


# ── 撞名加后缀重试一次 ────────────────────────────────────
def test_name_collision_suffix(db_path, monkeypatch):
    conn = db.get_conn()
    _add_atoms(conn, ["甲%d #c0" % i for i in range(5)] + ["乙%d #c1" % i for i in range(5)])
    _fake_embed(monkeypatch, {"#c0": _oh(0), "#c1": _oh(1)})
    _zhipu(monkeypatch, [_arr([
        {"id": 0, "name": "同名词", "description": "甲"},
        {"id": 1, "name": "同名词", "description": "乙"},
    ])])
    r = td.mint_semantic_tags(db_path)
    assert set(_names(conn)) == {"同名词", "同名词·2"}
    assert r["levels"]["1"]["tags"] == 2


# ── 幂等重跑: 成员重叠过半 → 复用既有 tag, 零新增行 ──────────
def test_idempotent_rerun_reuses_by_overlap(db_path, monkeypatch):
    conn = db.get_conn()
    _add_atoms(conn, ["甲%d #c0" % i for i in range(5)] + ["乙%d #c1" % i for i in range(5)])
    _fake_embed(monkeypatch, {"#c0": _oh(0), "#c1": _oh(1)})
    resp = [_arr([{"id": 0, "name": "标签甲", "description": "甲"},
                  {"id": 1, "name": "标签乙", "description": "乙"}])]
    _zhipu(monkeypatch, resp + list(resp))  # 两轮同应答
    td.mint_semantic_tags(db_path)
    r2 = td.mint_semantic_tags(db_path)
    assert set(_names(conn)) == {"标签甲", "标签乙"}  # 无 ·2 重复
    assert r2["levels"] == {"1": {"tags": 2, "mounts": 0}}  # INSERT OR IGNORE 零新增
    assert conn.execute("SELECT COUNT(*) FROM tag_mount").fetchone()[0] == 10


# ── 层级第二层涌现 + parent_of ─────────────────────────────
def test_hierarchy_level2_emerges(db_path, monkeypatch):
    conn = db.get_conn()
    # A=5 句互 cos1.0; B=4 句互 cos1.0; 桥句与 A cos0.61 / 与 B 0.79 →
    # louvain 分两簇, 簇间跨边计数 >2 连商图边 → L2 收编两 L1。
    _add_atoms(conn, ["甲%d #c0" % i for i in range(5)]
               + ["乙%d #c1" % i for i in range(4)] + ["桥句 #br"])
    _fake_embed(monkeypatch, {"#c0": _oh(0), "#c1": _oh(1), "#br": _bridge(0.61)})
    fake = _zhipu(monkeypatch, [
        _arr([{"id": 0, "name": "标签甲", "description": "甲"},
              {"id": 1, "name": "标签乙", "description": "乙"}]),
        _arr([{"id": 0, "name": "上层主题", "description": "总括"}]),
    ])
    r = td.mint_semantic_tags(db_path)
    assert r["levels"]["1"]["tags"] == 2 and r["levels"]["2"]["tags"] == 1
    assert r["levels"]["2"]["mounts"] == 0  # L2 不直接挂 atom
    l2 = conn.execute("SELECT id FROM tag WHERE level=2").fetchone()[0]
    parents = {row[0] for row in conn.execute(
        "SELECT parent_id FROM tag WHERE level=1")}
    assert parents == {l2}
    assert len(fake.calls) == 2
    # 幂等重跑: L2 同名同层复用, 层级结构不变
    _zhipu(monkeypatch, [
        _arr([{"id": 0, "name": "标签甲", "description": "甲"},
              {"id": 1, "name": "标签乙", "description": "乙"}]),
        _arr([{"id": 0, "name": "上层主题", "description": "总括"}]),
    ])
    r2 = td.mint_semantic_tags(db_path)
    assert r2["levels"] == {"1": {"tags": 2, "mounts": 0},
                            "2": {"tags": 1, "mounts": 0}}
    assert conn.execute("SELECT COUNT(*) FROM tag").fetchone()[0] == 3


# ── 新 atom 挂载 ──────────────────────────────────────────
def test_mount_new_atoms_nearest_above_threshold(db_path, monkeypatch):
    conn = db.get_conn()
    _add_atoms(conn, ["甲%d #c0" % i for i in range(5)] + ["乙%d #c1" % i for i in range(5)])
    _fake_embed(monkeypatch, {"#c0": _oh(0), "#c1": _oh(1),
                              "#d0": _oh(4), "#d1": _oh(5)})
    _zhipu(monkeypatch, [_arr([
        {"id": 0, "name": "标签甲", "description": "甲主题 #d0"},
        {"id": 1, "name": "标签乙", "description": "乙主题 #d1"},
    ])])
    td.mint_semantic_tags(db_path)
    # 新 atom: 命中标签甲描述(cos1.0) / 无关(cos0) / 低于阈值(0.4) / needs_embed 候选
    _add_atoms(conn, ["新句一 #d0", "无关句 #zz", "低相似句 #low", "欠向量句 #d0"],
               needs_embed=0)
    conn.execute("UPDATE atom SET needs_embed=1 WHERE text='欠向量句 #d0'")
    monkeypatch.setattr(embedding, "embed_batch",
                        _fake_embed_batch({"#d0": _oh(4), "#d1": _oh(5),
                                           "#low": _bridge2(0.4)}))
    n = td.mount_new_atoms(db_path)
    assert n == 2  # 新句一 + 欠向量句 (needs_embed 候选通道)
    rows = conn.execute(
        "SELECT a.text, t.name, m.w FROM tag_mount m "
        "JOIN atom a ON a.id=m.atom_id JOIN tag t ON t.id=m.tag_id "
        "WHERE a.text IN ('新句一 #d0','欠向量句 #d0')").fetchall()
    assert {r[0] for r in rows} == {"新句一 #d0", "欠向量句 #d0"}
    assert all(r[1] == "标签甲" and abs(r[2] - 1.0) < 1e-6 for r in rows)
    assert conn.execute("SELECT COUNT(*) FROM tag_mount").fetchone()[0] == 12
    assert td.mount_new_atoms(db_path) == 0  # 已挂 → 幂等


def _fake_embed_batch(mapping):
    def batch(texts):
        out = []
        for t in texts:
            hit = next((v for mk, v in mapping.items() if mk in t), None)
            out.append(list(hit) if hit is not None else _oh(11))
        return out
    return batch


def _bridge2(cos):
    """与 _oh(4) 的 cos=cos 的单位向量 (与 _oh(5)/_oh(11) 正交)。"""
    v = [0.0] * DIM
    v[4] = cos
    v[6] = (1.0 - cos * cos) ** 0.5
    return v


# ── 空库/簇不足: 零 LLM 零写入 ─────────────────────────────
def test_mint_no_clusters_zero_calls(db_path, monkeypatch):
    conn = db.get_conn()
    _add_atoms(conn, ["孤句一 #c0", "孤句二 #c1"])
    _fake_embed(monkeypatch, {"#c0": _oh(0), "#c1": _oh(1)})
    fake = _zhipu(monkeypatch, [])
    r = td.mint_semantic_tags(db_path)
    assert r["clusters_kept"] == 0 and r["tags"] == []
    assert fake.calls == []
    assert conn.execute("SELECT COUNT(*) FROM tag_mount").fetchone()[0] == 0


def test_mount_no_semantic_tags_zero(db_path, monkeypatch):
    conn = db.get_conn()
    _add_atoms(conn, ["句 #c0"])
    _fake_embed(monkeypatch, {"#c0": _oh(0)})
    assert td.mount_new_atoms(db_path) == 0


# ── CLI dry-run: 零 LLM ───────────────────────────────────
def test_cli_dry_run_no_llm(db_path, monkeypatch, capsys):
    conn = db.get_conn()
    _add_atoms(conn, ["甲%d #c0" % i for i in range(5)] + ["乙%d #c1" % i for i in range(5)])
    _fake_embed(monkeypatch, {"#c0": _oh(0), "#c1": _oh(1)})
    fake = _zhipu(monkeypatch, [])
    assert td.main([db_path]) == 0
    out = capsys.readouterr().out
    assert "communities" in out and "tag_mount: 0" in out
    assert fake.calls == []
