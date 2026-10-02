"""H6 atom 召回面测试: atom 源召回 / tag 遍历腿 / 双时态排除 / legacy 回切 /
空 atom 库优雅返回 / 向量腿 / gate 校准阈值消费 / with_tag 契约 / vec_atom 面。

tmp db 隔离 (db.init(tmp_path) 切连接); 零网络 (embedding/gate/laya 全 mock);
不触生产 data/memory.db。conftest autouse 已 pin MEM_LAYA_ENABLED=0。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 仓根平铺模块

import db  # noqa: E402
import embedding  # noqa: E402
import gate  # noqa: E402
import laya_client  # noqa: E402
import recall as recall_mod  # noqa: E402
import store  # noqa: E402
import vec_index  # noqa: E402

DIM = 64  # 小维度 (monkeypatch vec_index.VEC_DIM 后 db.init 建表)


def _init(tmp_path, monkeypatch):
    monkeypatch.setattr(vec_index, "VEC_DIM", DIM)
    db.init(tmp_path / "mem.db")
    return db.get_conn()


def _atom(conn, text, *, p_dur=0.6, valid_from=None, valid_to=None,
          source_cwd=None, label="fact", subjects=None):
    if valid_from is None:  # 缺省=now: freshness 默认开下既有用例分数零漂移
        valid_from = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    cur = conn.execute(
        "INSERT INTO atom(text, label, p_dur, valid_from, valid_to, source_cwd, subjects) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (text, label, p_dur, valid_from, valid_to, source_cwd, subjects))
    return cur.lastrowid


def _edge(conn, a, b, w=0.9, kind="related"):
    conn.execute("INSERT INTO atom_edge(a_id,b_id,w,kind) VALUES(?,?,?,?)",
                 (min(a, b), max(a, b), w, kind))  # CHECK(a_id < b_id)


def _sem_tag(conn, name, members, w=0.9):
    conn.execute(
        "INSERT INTO tag(name, kind, level, description) "
        "VALUES(?, 'semantic', 1, '')", (name,))
    tid = conn.execute("SELECT id FROM tag WHERE name=? AND level=1",
                       (name,)).fetchone()[0]
    for aid in members:
        conn.execute("INSERT INTO tag_mount(tag_id, atom_id, w) VALUES(?,?,?)",
                     (tid, aid, w))
    return tid


def _ids(res):
    res = res["results"] if isinstance(res, dict) else res
    return [f["id"] for f in (r["fact"] if isinstance(r, dict) and "fact" in r
                              else r for r in res)]


# ── 1. atom 源召回: 文本腿 + 输出 shape ─────────────────────────────

def test_atom_text_leg_and_shape(tmp_path, monkeypatch):
    conn = _init(tmp_path, monkeypatch)
    a1 = _atom(conn, "sqlite-vec 部署需要 pip install sqlite-vec==0.1.9")
    _atom(conn, "kubernetes GPU 节点池配置")
    res = recall_mod.recall("sqlite 部署")
    assert isinstance(res, list) and len(res) == 1
    f = res[0]
    assert f["id"] == f"atom:{a1}" and f["text"].startswith("sqlite-vec")
    assert f["label"] == "fact" and "subject_id" not in f  # 实体腿不进 atom 面
    assert f["_snaptag"]["kg_uri"] == f"kg://atom/{a1}"
    assert f["_snaptag"]["mem_path"] is None  # mem-*.md 投影是 fact 机制


def _pad(*dirs):
    return list(dirs) + [0.0] * (DIM - len(dirs))


def _mock_vecs(monkeypatch, qv, by_atom):
    """mock embedding.embed → qv; 并把 by_atom {aid: vec} 同步进 vec_atom。"""
    vec_index_backed = dict(by_atom)
    for aid, v in vec_index_backed.items():
        vec_index.sync_atom(aid, v)
    monkeypatch.setattr(embedding, "embed",
                        lambda text, providers=None: list(qv))


# ── 2. tag 遍历腿: 兄弟入场 / 排除已命中 / cap / 无向量惰性 ───────────

def test_tag_traversal_leg(tmp_path, monkeypatch):
    conn = _init(tmp_path, monkeypatch)
    hit = _atom(conn, "sqlite-vec 部署需要 pip install sqlite-vec==0.1.9")
    sib = _atom(conn, "数据库向量索引选型与降级路径")  # 无 query token 字面命中
    _sem_tag(conn, "向量检索", [hit, sib])
    qv = _pad(1.0, 0.0)
    _mock_vecs(monkeypatch, qv, {hit: _pad(1.0, 0.0),
                                 sib: _pad(0.7, 0.714142842854)})  # cos≈0.7
    res = recall_mod.recall("sqlite 部署", use_vec=True)
    ids = _ids(res)
    assert f"atom:{hit}" in ids and f"atom:{sib}" in ids, ids
    # hit (match+vec 面) 必须排在 sibling (仅 tag 翼) 前
    assert ids.index(f"atom:{hit}") < ids.index(f"atom:{sib}")
    # 无向量 (use_vec 关) → tag 腿惰性, 只有文本腿命中 (w×cos 公式无 cos 不扩张)
    assert _ids(recall_mod.recall("sqlite 部署")) == [f"atom:{hit}"]


def test_tag_sibling_cap_and_dedupe(tmp_path, monkeypatch):
    conn = _init(tmp_path, monkeypatch)
    hit = _atom(conn, "sqlite-vec 部署需要 pip install sqlite-vec==0.1.9")
    sibs = [_atom(conn, f"向量检索兄弟知识点 {i}") for i in range(12)]
    _sem_tag(conn, "向量检索", [hit] + sibs)
    qv = _pad(1.0, 0.0)
    # sibling cos=0.2 < VEC_MIN(0.3) → 不走向量腿, 只能从 tag 翼入场 (验 cap)
    _mock_vecs(monkeypatch, qv,
               {hit: _pad(1.0, 0.0),
                **{a: _pad(0.2, 0.979795897113) for a in sibs}})
    res = recall_mod.recall("sqlite 部署", use_vec=True)
    ids = _ids(res)
    assert len(ids) == 1 + recall_mod.TAG_SIBLING_CAP, ids  # 1 hit + cap 8


# ── 3. 双时态排除 ────────────────────────────────────────────────────

def test_bitemporal_exclusion(tmp_path, monkeypatch):
    conn = _init(tmp_path, monkeypatch)
    a1 = _atom(conn, "sqlite-vec 部署需要 pip install sqlite-vec==0.1.9",
               valid_from="2026-01-01T00:00:00+00:00")
    _atom(conn, "sqlite 旧部署方式已废弃",
          valid_from="2026-01-01T00:00:00+00:00",
          valid_to="2026-06-01T00:00:00+00:00")
    # 缺省: valid_to 非空 → 排除
    assert _ids(recall_mod.recall("sqlite 部署")) == [f"atom:{a1}"]
    # as_of 在有效窗内 → 历史时刻可见
    got = _ids(recall_mod.recall("sqlite 部署", as_of="2026-03-01T00:00:00+00:00"))
    assert len(got) == 2, got
    # as_of 在 valid_to 之后 → 不可见
    got = _ids(recall_mod.recall("sqlite 部署", as_of="2026-07-01T00:00:00+00:00"))
    assert got == [f"atom:{a1}"], got


# ── 4. legacy 回切 env (MEM_RECALL_LEGACY_FACT=1) ────────────────────

def test_legacy_env_switch(tmp_path, monkeypatch):
    conn = _init(tmp_path, monkeypatch)
    _atom(conn, "sqlite-vec 部署需要 pip install sqlite-vec==0.1.9")
    eid = store.put_entity("部署器", "concept")
    fid = store.put_fact(eid, "uses", "sqlite", extractor="llm", topic="部署器用 sqlite")
    # 缺省: atom 面
    assert _ids(recall_mod.recall("sqlite")) == ["atom:1"]
    monkeypatch.setenv("MEM_RECALL_LEGACY_FACT", "1")
    res = recall_mod.recall("sqlite")
    assert [f["id"] for f in res] == [fid], res  # fact 面回归, atom 不可见


# ── 5. 空 atom 库优雅返回 (自动回落 fact 面 / 全空 → []) ─────────────

def test_empty_atom_db_graceful(tmp_path, monkeypatch):
    conn = _init(tmp_path, monkeypatch)
    eid = store.put_entity("部署器", "concept")
    fid = store.put_fact(eid, "uses", "sqlite", extractor="llm", topic="部署器用 sqlite")
    # live atom 为空 → 回落 fact 面 (零回归护栏)
    res = recall_mod.recall("sqlite")
    assert [f["id"] for f in res] == [fid]
    # 全空库 → []
    db.init(tmp_path / "empty.db")
    assert recall_mod.recall("sqlite 部署") == []


# ── 6. 向量腿 (vec_atom ANN) ─────────────────────────────────────────

def test_vector_leg(tmp_path, monkeypatch):
    conn = _init(tmp_path, monkeypatch)

    def _pad(*dirs):
        return list(dirs) + [0.0] * (DIM - len(dirs))

    qv = _pad(1.0, 0.0)
    near = _pad(0.9, 0.435889894354)  # cos(qv)≈0.9; 归一后同向占优
    far = _pad(0.0, 1.0)
    a1 = _atom(conn, "container orchestration node pool")  # 无中文 token 命中
    _atom(conn, "unrelated english text about poetry")
    vec_index.sync_atom(a1, near)
    vec_index.sync_atom(2, far)
    monkeypatch.setattr(embedding, "embed", lambda text, providers=None: list(qv))
    res = recall_mod.recall("容器编排", use_vec=True)
    assert _ids(res) == [f"atom:{a1}"], res
    # embed 失败 (空向量) → 向量腿 passive 跳过, 不炸
    monkeypatch.setattr(embedding, "embed", lambda text, providers=None: [])
    assert recall_mod.recall("容器编排", use_vec=True) == []


# ── 7. gate: 校准阈值消费 / keep 入场 / None 弃翼 / A 路不动 ─────────

def _gate_fixture(tmp_path, monkeypatch):
    conn = _init(tmp_path, monkeypatch)
    hit = _atom(conn, "sqlite-vec 部署需要 pip install sqlite-vec==0.1.9")
    sib = _atom(conn, "数据库向量索引选型与降级路径")
    _sem_tag(conn, "向量检索", [hit, sib])
    qv = _pad(1.0, 0.0)
    # sib cos=0.25 < VEC_MIN → 只经 tag 翼入场 (gate 只判 B 翼)
    _mock_vecs(monkeypatch, qv, {hit: _pad(1.0, 0.0),
                                 sib: _pad(0.25, 0.968245836552)})
    return hit, sib


def test_gate_laya_uses_calibrated_p_keep(tmp_path, monkeypatch):
    hit, sib = _gate_fixture(tmp_path, monkeypatch)
    seen = {}

    def fake_laya(cand_texts, query, anchors, p_keep=0.35):
        seen["p_keep"] = p_keep
        seen["anchors"] = anchors
        return {fid: {"keep": True, "match_score": 0.8,
                      "matched_anchor": "sqlite", "p_high": 0.9}
                for fid in cand_texts}

    monkeypatch.setattr(laya_client, "laya_available", lambda: True)
    monkeypatch.setattr(gate, "run_gate_laya", fake_laya)
    res = recall_mod.recall("sqlite 部署", use_vec=True, use_gate=True)
    by_id = {f["id"]: f for f in res}
    assert seen["p_keep"] == recall_mod.GATE_P_HIGH_KEEP  # 校准阈值被消费
    assert by_id[f"atom:{sib}"]["gate_keep"] is True
    assert by_id[f"atom:{sib}"]["match_score"] == 0.8
    assert "gate_keep" not in by_id[f"atom:{hit}"]  # A 路永不带 gate 键


def test_gate_none_drops_tag_wing_only(tmp_path, monkeypatch):
    hit, sib = _gate_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(laya_client, "laya_available", lambda: True)
    monkeypatch.setattr(gate, "run_gate_laya",
                        lambda *a, **k: None)  # laya 整批败
    monkeypatch.setattr(gate, "run_gate",
                        lambda *a, **k: (_ for _ in ()).throw(
                            gate.GateFailed("断供")))  # 回落亦败
    ids = _ids(recall_mod.recall("sqlite 部署", use_vec=True, use_gate=True))
    assert ids == [f"atom:{hit}"], ids  # B 翼全弃, A 路照常


def test_run_gate_laya_p_keep_and_p_high(tmp_path, monkeypatch):
    """gate.py 面: p_keep 覆盖缺省 0.35 + verdicts 附原始 p_high。"""
    ans = {"a1": {"score": 1.0, "probabilities": {"0": 0.1, "1": 0.6, "2": 0.3}}}
    monkeypatch.setattr(laya_client, "laya_batch",
                        lambda state, questions, timeout=30.0: ans)
    v = gate.run_gate_laya({"a1": "sqlite 部署文档"}, "sqlite",
                           {"sqlite"}, p_keep=0.2)
    assert v["a1"]["p_high"] == 0.3
    assert v["a1"]["keep"] is True  # 0.3 ≥ 0.2 (缺省 0.35 会判 False)


# ── 8. hook 注入契约 (recall_inject 消费面) 不变 ─────────────────────

def test_with_tag_envelope_contract(tmp_path, monkeypatch):
    conn = _init(tmp_path, monkeypatch)
    a1 = _atom(conn, "sqlite-vec 部署需要 pip install sqlite-vec==0.1.9")
    res = recall_mod.recall("sqlite 部署", session_id="s1", with_tag=True)
    assert set(res) == {"query", "session_id", "suggest_bfs", "results"}
    r0 = res["results"][0]
    assert set(r0) == {"fact", "score", "tag"}
    assert r0["fact"]["id"] == f"atom:{a1}"
    assert r0["tag"]["fact_id"] == f"atom:{a1}"


# ── 9. vec_atom 面: sync / 维度防御 / backfill / distill passive ────

def test_vec_atom_sync_and_backfill(tmp_path, monkeypatch):
    conn = _init(tmp_path, monkeypatch)
    a1 = _atom(conn, "sqlite-vec 部署")
    a2 = _atom(conn, "已清算 atom", valid_to="2026-06-01T00:00:00+00:00")
    _atom(conn, "向量回填 atom")
    # 维度不匹配 → passive 跳过 (数据条件, 不炸)
    vec_index.sync_atom(a1, [0.1, 0.2])
    assert conn.execute("SELECT COUNT(*) FROM vec_atom").fetchone()[0] == 0
    v = [1.0] + [0.0] * (DIM - 1)
    monkeypatch.setattr(embedding, "embed_batch",
                        lambda texts, providers=None: [list(v) for _ in texts])
    out = vec_index.backfill_atoms()
    assert out["atoms"] == 2, out  # live 2 (a1 + a3), valid_to 已清的不回填
    got = conn.execute("SELECT COUNT(*) FROM vec_atom").fetchone()[0]
    assert got == 2
    assert vec_index.backfill_atoms()["atoms"] == 2  # 幂等
    # backfill_all 亦覆盖 atom 命名空间
    conn.execute("DELETE FROM vec_atom")
    conn.commit()
    assert vec_index.backfill_all()["atoms"] == 2
    top = vec_index.atom_topk(list(v), 5)
    assert [a for a, _ in top] == [a1, 3] or [a for a, _ in top] == [3, a1]


def test_distill_sync_atom_vecs_passive(tmp_path, monkeypatch):
    """distill H6 钩子: 维度不匹配 / vec 面异常 → passive 不炸。"""
    from src.distill import _sync_atom_vecs
    _init(tmp_path, monkeypatch)
    _sync_atom_vecs([(1, [0.1])])  # 短向量 → sync_atom 内部跳过
    _sync_atom_vecs([(1, None)])
    assert True  # 到此即 passive 证明


def test_recall_scored_with_cwd_filter(tmp_path, monkeypatch):
    conn = _init(tmp_path, monkeypatch)
    a1 = _atom(conn, "sqlite-vec 部署", source_cwd="/home/yy/projects/x")
    _atom(conn, "sqlite-vec 别库条目", source_cwd="/home/yy/projects/y")
    _atom(conn, "sqlite-vec 无cwd 老数据", source_cwd=None)
    ids = _ids(recall_mod.recall("sqlite", cwd="/home/yy/projects/x"))
    assert set(ids) == {"atom:1", "atom:3"}, ids  # 异 cwd 排除, NULL 兼容


# ── 10. v3: subjects 锚定 / related 第四腿 / freshness / parent 爬层 ──

def test_subjects_anchor(tmp_path, monkeypatch):
    """subjects 锚定: 精确符号是 query 子串 → text 无 token 命中亦入候选;
    裸串 subjects 容错; 无关符号/无 subjects 不入场; v3 三列透传。"""
    conn = _init(tmp_path, monkeypatch)
    a1 = _atom(conn, "命令行工具的用法速查", subjects='["memsvc-cli"]')
    _atom(conn, "另一条命令行工具说明", subjects='["other-tool"]')
    a3 = _atom(conn, "裸串 subjects 容错条目", subjects="memsvc-cli")  # 非 JSON
    _atom(conn, "无 subjects 条目")
    ids = set(_ids(recall_mod.recall("memsvc-cli 安装在哪", min_score=0.0)))
    assert ids == {f"atom:{a1}", f"atom:{a3}"}, ids
    by_id = {f["id"]: f for f in recall_mod.recall(
        "memsvc-cli 安装在哪", min_score=0.0)}
    assert by_id[f"atom:{a1}"]["subjects"] == '["memsvc-cli"]'  # 行透传
    assert by_id[f"atom:{a1}"]["event_at"] is None
    assert by_id[f"atom:{a1}"]["last_seen_at"] is None


def _qv(monkeypatch):
    """边/tag 翼惰性门 (v3 对抗审查 major): 无 query 向量不扩张 — 测试给 qv。"""
    import embedding as _emb
    v = [1.0] + [0.0] * (DIM - 1)
    monkeypatch.setattr(_emb, "embed", lambda q: list(v))


def test_related_edge_wing(tmp_path, monkeypatch):
    """related 第四腿: 边邻居经 tag_wing 通道入场 (bypass 地板; 需 query
    向量 — 惰性门与 tag 腿同先例); w<0.5 / 对端已清算 (valid_to 非空) 不入场。"""
    conn = _init(tmp_path, monkeypatch)
    _qv(monkeypatch)
    hit = _atom(conn, "sqlite-vec 部署需要 pip install sqlite-vec==0.1.9")
    nb = _atom(conn, "数据库选型时的权衡笔记")  # 无 query token 命中, 仅经边入场
    weak = _atom(conn, "低权重边邻居不该出现")
    dead = _atom(conn, "已清算边邻居不该出现",
                 valid_from="2026-01-01T00:00:00+00:00",
                 valid_to="2026-06-01T00:00:00+00:00")
    _edge(conn, hit, nb, w=0.9, kind="related")
    _edge(conn, hit, weak, w=0.3)  # w < 0.5 → 不入场
    _edge(conn, hit, dead, w=0.9, kind="supersedes")  # 对端非 live → 不入场
    ids = set(_ids(recall_mod.recall("sqlite 部署", use_vec=True)))
    assert ids == {f"atom:{hit}", f"atom:{nb}"}, ids


def test_related_edge_caps(tmp_path, monkeypatch):
    """related 第四腿受帽: 每 hit 帽 EDGE_WING_PER_HIT / 总帽 EDGE_WING_CAP,
    w 高者优先 (i=0 最低 w 的邻居全被 per-hit 帽挡掉)。"""
    conn = _init(tmp_path, monkeypatch)
    _qv(monkeypatch)
    hits = [_atom(conn, f"sqlite-vec 部署第{c}条") for c in "一二三"]
    nb_ids: dict[tuple[int, int], int] = {}
    for h in hits:
        for i in range(5):
            n = _atom(conn, f"边邻居文本{h}-{i}号")
            _edge(conn, h, n, w=0.6 + 0.01 * i)
            nb_ids[(h, i)] = n
    ids = set(_ids(recall_mod.recall("sqlite 部署", use_vec=True)))
    assert len(ids) == len(hits) + recall_mod.EDGE_WING_CAP, ids
    per = {h: sum(1 for (hh, _), n in nb_ids.items()
                  if hh == h and f"atom:{n}" in ids) for h in hits}
    assert all(c <= recall_mod.EDGE_WING_PER_HIT for c in per.values()), per
    assert sum(per.values()) == recall_mod.EDGE_WING_CAP
    assert all(f"atom:{nb_ids[(h, 0)]}" not in ids for h in hits)  # w 最低挡掉


def test_freshness_fact_only(tmp_path, monkeypatch):
    """freshness: 只衰减 fact (0.5**(age/90) 乘进 LIF/confidence 先验);
    judgment 不衰减; valid_from 解析失败不衰减; env MEM_RECALL_FRESH_OFF=1 关。"""
    monkeypatch.delenv("MEM_RECALL_FRESH_OFF", raising=False)
    conn = _init(tmp_path, monkeypatch)
    old = _atom(conn, "sqlite-vec 部署旧记", valid_from="2026-01-01T00:00:00+00:00")
    recent_vf = (datetime.now(timezone.utc).replace(microsecond=0)
                 - timedelta(hours=1)).isoformat()
    new = _atom(conn, "sqlite-vec 部署新记", valid_from=recent_vf)
    jd = _atom(conn, "sqlite-vec 部署经验判断", label="judgment",
               valid_from="2026-01-01T00:00:00+00:00")
    bad = _atom(conn, "sqlite-vec 部署坏日期", valid_from="not-a-date")
    by_id = {f["id"]: f for f in
             recall_mod.recall("sqlite 部署", min_score=0.0)}
    assert by_id[f"atom:{old}"]["LIF"] < 0.3  # ~0.6·0.5^(274/90)≈0.07
    assert by_id[f"atom:{new}"]["LIF"] > 0.55  # 1h 龄 ≈ 无衰减
    assert by_id[f"atom:{jd}"]["LIF"] == pytest.approx(0.6)  # 非 fact 不衰减
    assert by_id[f"atom:{bad}"]["LIF"] == pytest.approx(0.6)  # 解析失败跳过
    monkeypatch.setenv("MEM_RECALL_FRESH_OFF", "1")
    by_id = {f["id"]: f for f in
             recall_mod.recall("sqlite 部署", min_score=0.0)}
    assert by_id[f"atom:{old}"]["LIF"] == pytest.approx(0.6)  # env 关 → 全不衰减


def test_tag_parent_climb(tmp_path, monkeypatch):
    """tag parent 爬层: 直系兄弟不足帽时, MEM_TAG_WING_PARENT=1 才上爬一层
    拉父的其他子 tag 下原子补足; 缺省关 (零行为变化)。"""
    conn = _init(tmp_path, monkeypatch)
    hit = _atom(conn, "sqlite-vec 部署需要 pip install sqlite-vec==0.1.9")
    sib = _atom(conn, "数据库向量索引选型与降级路径")  # 父层兄弟, 无 token 命中

    def _tag(name, level, parent=None):
        conn.execute(
            "INSERT INTO tag(name, kind, level, description, parent_id) "
            "VALUES(?, 'semantic', ?, '', ?)", (name, level, parent))
        return conn.execute("SELECT id FROM tag WHERE name=? AND level=?",
                            (name, level)).fetchone()[0]

    p = _tag("检索父域", 1)
    c1 = _tag("向量检索直系", 2, parent=p)  # 只挂 hit → 直系兄弟为空
    c2 = _tag("向量检索旁支", 2, parent=p)  # 只挂 sib → 仅爬层可达
    conn.execute("INSERT INTO tag_mount(tag_id, atom_id, w) VALUES(?,?,0.9)", (c1, hit))
    conn.execute("INSERT INTO tag_mount(tag_id, atom_id, w) VALUES(?,?,0.9)", (c2, sib))
    qv = _pad(1.0, 0.0)
    # sib cos=0.25 < VEC_MIN(0.3) → 向量腿不带入场, 只能经爬层 tag_wing 进
    _mock_vecs(monkeypatch, qv, {hit: _pad(1.0, 0.0),
                                 sib: _pad(0.25, 0.968245836552)})
    # 缺省关: 直系无兄弟 → 只有文本腿命中
    assert _ids(recall_mod.recall("sqlite 部署", use_vec=True)) == [f"atom:{hit}"]
    # env 开: 爬 parent → c2 下 sib 补足入场 (tag_wing 通道)
    monkeypatch.setenv("MEM_TAG_WING_PARENT", "1")
    ids = set(_ids(recall_mod.recall("sqlite 部署", use_vec=True)))
    assert ids == {f"atom:{hit}", f"atom:{sib}"}, ids


# ── v3 laya rerank: 仅向量腿入场的语义泛化候选交 laya 批判 ──────────

def test_laya_rerank_vec_only(tmp_path, monkeypatch):
    """MEM_LAYA_RERANK (缺省开, =0 关): 仅向量腿入场 (无 token/subjects/翼 命中) 的候选
    交 laya — keep=False 剔除, keep 的 score 乘 (0.5+0.5·match_score) 重排;
    文本直命中不参与; env 缺省关 (零回归)。"""
    import os
    conn = _init(tmp_path, monkeypatch)
    hit = _atom(conn, "sqlite-vec 部署需要 pip install sqlite-vec==0.1.9")
    vec_only = _atom(conn, "霍顿旅行车 stealth camp 选型结论")  # 无 query token 命中
    qv = _pad(1.0, 0.0)
    _mock_vecs(monkeypatch, qv, {hit: _pad(1.0, 0.0),
                                 vec_only: _pad(0.8, 0.6)})

    calls = {}

    def fake_laya(cand_texts, query, anchors, p_keep=0.35):
        calls["ids"] = set(cand_texts)
        return {fid: {"keep": True, "match_score": 0.8}
                for fid in cand_texts}

    monkeypatch.setattr(laya_client, "laya_available", lambda: True)
    monkeypatch.setattr(gate, "run_gate_laya", fake_laya)
    monkeypatch.setenv("MEM_LAYA_RERANK", "1")
    res = recall_mod.recall("sqlite 部署", use_vec=True, min_score=0.0)
    ids = _ids(res)
    assert ids == [f"atom:{hit}", f"atom:{vec_only}"], ids
    assert calls["ids"] == {f"atom:{vec_only}"}  # 只审 vec-only, 不审文本命中

    # laya 判不相关 (keep=False) → vec-only 剔除, 文本命中保留
    monkeypatch.setattr(gate, "run_gate_laya",
                        lambda *a, **k: {f"atom:{vec_only}":
                                         {"keep": False, "match_score": 0.1}})
    assert _ids(recall_mod.recall("sqlite 部署", use_vec=True,
                                  min_score=0.0)) == [f"atom:{hit}"]

    # laya 整批失败 → 原 score 原样 (rerank 失败不降级)
    monkeypatch.setattr(gate, "run_gate_laya", lambda *a, **k: None)
    assert set(_ids(recall_mod.recall("sqlite 部署", use_vec=True,
                                       min_score=0.0))) == \
        {f"atom:{hit}", f"atom:{vec_only}"}

    # env 显式关 (MEM_LAYA_RERANK=0) → laya 不被调用, vec-only 照常返回
    monkeypatch.setenv("MEM_LAYA_RERANK", "0")
    assert set(_ids(recall_mod.recall("sqlite 部署", use_vec=True,
                                       min_score=0.0))) == \
        {f"atom:{hit}", f"atom:{vec_only}"}
