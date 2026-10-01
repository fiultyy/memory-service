"""H4 tag_dream 测试: mock embedding/zhipu/laya (零网络/零生产库), tmp db 隔离。

覆盖: 聚类+挂载 / 小簇丢弃 / 撞名后缀 / 幂等重跑(成员重叠复用) /
层级第二层涌现+parent_of / 新 atom 挂载(阈值+needs_embed 候选) /
空库零调用 / dry-run 零 LLM / 挂账#1 laya 审计(audit_mounts) +
top-3 竞争挂载(mount_new_atoms use_laya)。
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 仓根平铺模块

import db  # noqa: E402
import embedding  # noqa: E402
import laya_client  # noqa: E402
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


def _mix(cos_a, cos_b):
    """与 _oh(4) cos=cos_a、_oh(5) cos=cos_b 的单位向量 (余维补正交)。"""
    s = (1.0 - cos_a * cos_a - cos_b * cos_b) ** 0.5
    v = [0.0] * DIM
    v[4], v[5], v[6] = cos_a, cos_b, s
    return v


# ── 挂账#1: laya 审计 + 竞争挂载 (全 mock, 零网络) ──────────
def _laya(monkeypatch, available=True, batch=None):
    """pin laya 可用性 + 注入 laya_batch mock (tag_dream 经 laya_client 模块
    属性调用, conftest autouse pin env=0 不影响函数级 patch)。"""
    monkeypatch.setattr(laya_client, "laya_available", lambda: available)
    if batch is not None:
        monkeypatch.setattr(laya_client, "laya_batch", batch)


def _laya_batch_by_marker(marker, low=0.1, high=0.6, calls=None):
    """内容驱动 mock: instructions 含 marker → same-topic=low, 否则 high
    (内容驱动免依赖问序)。"""

    def batch(state, questions, timeout=30.0):
        out = {}
        for k, q in questions.items():
            if calls is not None:
                calls.append(q)
            v = low if marker in q["instructions"] else high
            out[k] = {"probabilities": {"0": 0.05, "1": 0.05, "2": v}}
        return out

    return batch


def _sem_tag(conn, name, desc):
    return conn.execute(
        "INSERT INTO tag(name, kind, level, description) "
        "VALUES(?, 'semantic', 1, ?)", (name, desc)).lastrowid


def _seed_mounts(conn):
    """两 semantic tag × 三挂载 (w=1.0): #low/#low2 待卸载, #ok 待降权。"""
    t1 = _sem_tag(conn, "标签甲", "甲主题")
    t2 = _sem_tag(conn, "标签乙", "乙主题")
    _add_atoms(conn, ["低分句 #low", "高分句 #ok", "另一低分 #low2"])
    for tid, txt in ((t1, "低分句 #low"), (t1, "高分句 #ok"), (t2, "另一低分 #low2")):
        aid = conn.execute("SELECT id FROM atom WHERE text=?", (txt,)).fetchone()[0]
        conn.execute("INSERT INTO tag_mount(tag_id, atom_id, w) VALUES(?, ?, 1.0)",
                     (tid, aid))


def test_audit_low_deleted_high_reweighted(db_path, monkeypatch):
    conn = db.get_conn()
    _seed_mounts(conn)
    _laya(monkeypatch, batch=_laya_batch_by_marker("#low"))
    n = td.audit_mounts(db_path)
    assert n == 3  # 2 卸载 + 1 降权
    rows = {(r[0], round(r[1], 4)) for r in conn.execute(
        "SELECT a.text, m.w FROM tag_mount m JOIN atom a ON a.id=m.atom_id")}
    assert rows == {("高分句 #ok", 0.6)}  # 低分行 DELETE, 高分行 w=1.0→0.6


def test_audit_laya_unavailable_zero_writes(db_path, monkeypatch):
    conn = db.get_conn()
    _seed_mounts(conn)
    def _boom(state, questions, timeout=30.0):
        raise AssertionError("laya 不可用时不应发批")
    _laya(monkeypatch, available=False, batch=_boom)
    assert td.audit_mounts(db_path) == 0
    assert tuple(conn.execute("SELECT COUNT(*), SUM(w) FROM tag_mount"
                              ).fetchone()) == (3, 3.0)  # 零 DB 变更


def test_audit_malformed_answers_skipped(db_path, monkeypatch):
    conn = db.get_conn()
    _seed_mounts(conn)

    def batch(state, questions, timeout=30.0):
        out = {}
        for k, q in questions.items():
            ins = q["instructions"]
            if "#bad1" in ins:
                out[k] = {"probabilities": "not-a-dict"}  # 非 dict
            elif "#bad2" in ins:
                pass  # 缺 answer 键
            else:
                out[k] = {"probabilities": {"2": 0.6}}
        return out

    t = _sem_tag(conn, "标签丙", "丙主题")
    _add_atoms(conn, ["畸形一 #bad1", "畸形二 #bad2"])
    for txt in ("畸形一 #bad1", "畸形二 #bad2"):
        aid = conn.execute("SELECT id FROM atom WHERE text=?", (txt,)).fetchone()[0]
        conn.execute("INSERT INTO tag_mount(tag_id, atom_id, w) VALUES(?, ?, 1.0)",
                     (t, aid))
    _laya(monkeypatch, batch=batch)
    n = td.audit_mounts(db_path)
    assert n == 3  # 仅正常答案处置 (3 seeded 行); 畸形跳过不炸
    ws = {r[0]: r[1] for r in conn.execute(
        "SELECT a.text, m.w FROM tag_mount m JOIN atom a ON a.id=m.atom_id")}
    assert ws["畸形一 #bad1"] == 1.0 and ws["畸形二 #bad2"] == 1.0  # 原样不动
    assert ws["高分句 #ok"] == 0.6


def test_audit_limit_truncates(db_path, monkeypatch):
    conn = db.get_conn()
    _seed_mounts(conn)
    calls = []
    _laya(monkeypatch, batch=_laya_batch_by_marker("#low", calls=calls))
    n = td.audit_mounts(db_path, limit=2)
    assert n == 2 and len(calls) == 2  # 只送评 2 问
    # 截断保留第三行 (t2/#low2) 原样; 已扫两行 = #low 卸载 + #ok 降权 0.6
    ws = {r[0]: r[1] for r in conn.execute(
        "SELECT a.text, m.w FROM tag_mount m JOIN atom a ON a.id=m.atom_id")}
    assert ws == {"高分句 #ok": 0.6, "另一低分 #low2": 1.0}


def _seed_two_tags(db_path, monkeypatch):
    """mint 两 semantic tag (描述向量 _oh(4)/_oh(5)), 返回 conn。"""
    conn = db.get_conn()
    _add_atoms(conn, ["甲%d #c0" % i for i in range(5)] + ["乙%d #c1" % i for i in range(5)])
    _fake_embed(monkeypatch, {"#c0": _oh(0), "#c1": _oh(1), "#d0": _oh(4), "#d1": _oh(5)})
    _zhipu(monkeypatch, [_arr([
        {"id": 0, "name": "标签甲", "description": "甲主题 #d0"},
        {"id": 1, "name": "标签乙", "description": "乙主题 #d1"},
    ])])
    td.mint_semantic_tags(db_path)
    return conn


def test_mount_laya_top3_competition(db_path, monkeypatch):
    conn = _seed_two_tags(db_path, monkeypatch)
    # 新 atom 与标签甲 cos0.7 / 标签乙 cos0.6 (均过 0.55) → 两候选同批竞争
    _add_atoms(conn, ["竞争句 #race"])
    monkeypatch.setattr(embedding, "embed_batch",
                        _fake_embed_batch({"#d0": _oh(4), "#d1": _oh(5),
                                           "#race": _mix(0.7, 0.6)}))
    calls = []

    def batch(state, questions, timeout=30.0):
        calls.extend(questions.values())
        out = {}
        for k, q in questions.items():
            hi = "标签乙" in q["instructions"]  # 乙高分 / 甲低分
            out[k] = {"probabilities": {"0": 0.05, "1": 0.05, "2": 0.9 if hi else 0.1}}
        return out

    _laya(monkeypatch, batch=batch)
    n = td.mount_new_atoms(db_path)
    assert n == 1
    assert len(calls) == 2  # top-3 两候选都送评 (不足 3 取实有)
    r = conn.execute(
        "SELECT t.name, m.w FROM tag_mount m JOIN tag t ON t.id=m.tag_id "
        "JOIN atom a ON a.id=m.atom_id WHERE a.text='竞争句 #race'").fetchone()
    assert r[0] == "标签乙" and abs(r[1] - 0.9) < 1e-6  # 只挂高分, w=laya 分


def test_mount_laya_off_falls_back_nearest(db_path, monkeypatch):
    conn = _seed_two_tags(db_path, monkeypatch)
    _add_atoms(conn, ["回落句 #d0"])
    monkeypatch.setattr(embedding, "embed_batch",
                        _fake_embed_batch({"#d0": _oh(4), "#d1": _oh(5)}))
    def _boom(state, questions, timeout=30.0):
        raise AssertionError("laya off 不应发批")
    _laya(monkeypatch, available=False, batch=_boom)
    n = td.mount_new_atoms(db_path)
    assert n == 1  # 现行为原样: 最近 tag cos≥0.55 即挂 w=cos
    r = conn.execute(
        "SELECT t.name, m.w FROM tag_mount m JOIN tag t ON t.id=m.tag_id "
        "JOIN atom a ON a.id=m.atom_id WHERE a.text='回落句 #d0'").fetchone()
    assert r[0] == "标签甲" and abs(r[1] - 1.0) < 1e-6


def test_mount_laya_no_candidates_zero(db_path, monkeypatch):
    conn = _seed_two_tags(db_path, monkeypatch)
    _add_atoms(conn, ["无关句 #zz"])  # 默认向量 _oh(11) 与两 tag cos=0
    monkeypatch.setattr(embedding, "embed_batch",
                        _fake_embed_batch({"#d0": _oh(4), "#d1": _oh(5)}))
    def _boom(state, questions, timeout=30.0):
        raise AssertionError("无候选不应发批")
    _laya(monkeypatch, batch=_boom)
    assert td.mount_new_atoms(db_path) == 0


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


def test_audit_delete_cap_bounds_blast_radius(db_path, monkeypatch):
    """单轮卸载帽 (对抗审查 major): laya 评分回归全低分时 DELETE 有界,
    帽外低分行原样留存 (不降权不删), 留待下轮 — 爆炸半径护栏。"""
    conn = db.get_conn()
    _seed_mounts(conn)
    monkeypatch.setattr(td, "_AUDIT_DEL_CAP", 1)
    _laya(monkeypatch, batch=_laya_batch_by_marker("#low"))
    n = td.audit_mounts(db_path)
    assert n == 2  # 2 low: 帽 1 → 删 1 留 1 (留的不计 handled); 1 high 降权
    assert conn.execute("SELECT COUNT(*) FROM tag_mount").fetchone()[0] == 2
    ws = sorted(round(r[0], 2) for r in conn.execute("SELECT w FROM tag_mount"))
    assert ws == [0.6, 1.0]  # 留存 low 行 w 不动, high 行降为 0.6
