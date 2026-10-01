"""H2 distill 模块测试: mock 三引擎 (zhipu/laya/embed), tmp db 隔离。

覆盖: 正常路径+事实tag铸币+sha 幂等 / laya 不可用挂起 / 段内 supersede /
cos 合并 + 候选边 / 毒 JSON 重试与 DLQ / audit_pending / reembed_needing。
不触生产 data/memory.db (db.init(tmp_path) 切连接)。
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
import src.distill as distill  # noqa: E402

DIM = 128  # > _EMBED_DIM_MIN(100) 才过坏向量判据


def _onehot(i):
    v = [0.0] * DIM
    v[i] = 1.0
    return v


def _mix085():
    """与 _onehot(0) 的 cos = 0.85 (候选边带): 单位向量 [c, sqrt(1-c²)]。"""
    return [0.85, (1.0 - 0.85 ** 2) ** 0.5] + [0.0] * (DIM - 2)


_VECS = {"#vA": _onehot(0), "#vB": _onehot(1), "#vC": _mix085()}


class FakeZhipu:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, system, messages, max_tokens=1500, tools=None,
             tool_choice=None):
        self.calls.append(messages)
        assert self.responses, "意外的多余 zhipu 调用"
        return self.responses.pop(0)


def _fake_embed(monkeypatch):
    def batch(texts):
        out = []
        for t in texts:
            hit = next((v for mk, v in _VECS.items() if mk in t), None)
            if hit is None:  # 无标记 (audit/reembed 补扫路径) → 默认正交位
                hit = _onehot(2)
            out.append(list(hit))
        return out
    monkeypatch.setattr(embedding, "embed_batch", batch)


def _fake_laya(monkeypatch, dur=0.9, edge=0.7, malformed_dur=False):
    def batch(state, questions, timeout=30.0):
        answers = {}
        for qid, q in questions.items():
            if qid.startswith("dur_"):
                if malformed_dur:
                    answers[qid] = {"score": 2}  # 缺 probabilities → needs_audit
                else:
                    answers[qid] = {"score": 2, "probabilities":
                                    {"0": 0.05, "1": 0.05, "2": dur}}
            elif qid.startswith("edg_"):
                answers[qid] = {"score": 2, "probabilities":
                                {"0": 0.1, "1": 0.2, "2": edge}}
        return answers
    monkeypatch.setattr(laya_client, "laya_available", lambda: True)
    monkeypatch.setattr(laya_client, "laya_batch", batch)


def _zhipu_arr(items):
    return json.dumps(items, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


@pytest.fixture
def tdb(tmp_path):
    conn = db.init(tmp_path / "t.db")
    distill._ensure_tables(conn)
    return conn


def _setup(monkeypatch, zhipu_responses, **laya_kw):
    fake = FakeZhipu(zhipu_responses)
    monkeypatch.setattr(distill, "_get_zhipu", lambda: fake)
    _fake_embed(monkeypatch)
    _fake_laya(monkeypatch, **laya_kw)
    return fake


CWD = "/home/yy/projects/memory-service"


# ── 正常路径 ────────────────────────────────────────────────
def test_normal_path_mints_tags_and_is_idempotent(tdb, monkeypatch):
    fake = _setup(monkeypatch, [_zhipu_arr([
        {"id": 0, "summary": "结论甲 #vA", "label": "fact"},
        {"id": 1, "summary": "结论乙 #vB", "label": "judgment"},
    ])])
    r = distill.distill_segment("甲句一。乙句二。", "s1", CWD, "2026-10-01T00:00:00Z")
    assert (r["atoms"], r["edges"], r["merged"]) == (2, 0, 0)
    assert r["supersede_proposals"] == []
    assert len(fake.calls) == 1
    atoms = tdb.execute("SELECT * FROM atom ORDER BY id").fetchall()
    assert len(atoms) == 2
    assert all(a["p_dur"] == 0.9 and a["needs_audit"] == 0
               and a["needs_embed"] == 0 and a["valid_to"] is None
               and a["source_cwd"] == CWD for a in atoms)
    assert json.loads(atoms[0]["source_refs"]) == ["甲句一。"]
    # 事实tag铸币: session 恒铸 + cwd 白名单 repo: (kind=factual/level=1)
    tags = {r["name"] for r in tdb.execute("SELECT name FROM tag")}
    assert tags == {"session:s1", "repo:memory-service"}
    links = tdb.execute("SELECT COUNT(*) c FROM tag_mount").fetchone()["c"]
    assert links == 4  # 2 atom × 2 tag
    assert tdb.execute("SELECT status FROM distill_seen").fetchone()["status"] == "ok"
    # sha 幂等: 同文重放 (换 session/cwd 也不重复计费)
    r2 = distill.distill_segment("甲句一。乙句二。", "s2", "/tmp/x", "t2")
    assert r2["skipped"] == "seen" and r2["atoms"] == 0
    assert len(fake.calls) == 1  # 零重计费
    assert tdb.execute("SELECT COUNT(*) c FROM atom").fetchone()["c"] == 2


def test_cwd_outside_whitelist_mints_session_only(tdb, monkeypatch):
    _setup(monkeypatch, [_zhipu_arr([{"id": 0, "summary": "结论 #vB", "label": "fact"}])])
    distill.distill_segment("单句。", "s9", "/tmp/scratch", "t")
    tags = {r["name"] for r in tdb.execute("SELECT name FROM tag")}
    assert tags == {"session:s9"}  # /tmp 跳过 repo: (spec §六)


# ── laya 不可用挂起 ─────────────────────────────────────────
def test_laya_unavailable_raises_zero_write(tdb, monkeypatch):
    fake = _setup(monkeypatch, [])
    monkeypatch.setattr(laya_client, "laya_available", lambda: False)
    with pytest.raises(distill.LayaUnavailable):
        distill.distill_segment("句子一。", "s1", CWD, "t")
    # 图零变更 + 不落 seen (调用方可持段重放) + 零 LLM 计费
    assert tdb.execute("SELECT COUNT(*) c FROM atom").fetchone()["c"] == 0
    assert tdb.execute("SELECT COUNT(*) c FROM distill_seen").fetchone()["c"] == 0
    assert fake.calls == []


def test_laya_batch_none_raises_zero_write(tdb, monkeypatch):
    _setup(monkeypatch, [_zhipu_arr([{"id": 0, "summary": "结论 #vA", "label": "fact"}])])
    monkeypatch.setattr(laya_client, "laya_available", lambda: True)
    monkeypatch.setattr(laya_client, "laya_batch",
                        lambda state, qs, timeout=30.0: None)  # 两次 None
    with pytest.raises(distill.LayaUnavailable):
        distill.distill_segment("句子一。", "s1", CWD, "t")
    assert tdb.execute("SELECT COUNT(*) c FROM atom").fetchone()["c"] == 0


# ── 段内 supersede ──────────────────────────────────────────
def test_intra_segment_supersede(tdb, monkeypatch):
    _setup(monkeypatch, [_zhipu_arr([
        {"id": 0, "summary": "旧状态句 #vA", "label": "fact", "superseded_by": 1},
        {"id": 1, "summary": "新状态句 #vB", "label": "fact"},
    ])])
    r = distill.distill_segment("旧句。新句。", "s1", CWD, "t")
    assert r["atoms"] == 1  # 被取代句不入图
    assert r["supersede_proposals"] == [
        {"old_id": 0, "new_id": 1, "old": "旧状态句 #vA", "new": "新状态句 #vB"}]
    assert tdb.execute("SELECT text FROM atom").fetchone()[0] == "新状态句 #vB"


# ── cos 合并 + 候选边 ───────────────────────────────────────
def test_cos_merge_and_candidate_edge(tdb, monkeypatch):
    _setup(monkeypatch, [_zhipu_arr([{"id": 0, "summary": "种子句 #vA", "label": "fact"}])])
    distill.distill_segment("种子句。", "s1", CWD, "t1")
    seed_id = tdb.execute("SELECT id FROM atom").fetchone()[0]
    # 第二段: 一句与种子 cos=1.0 (并成员), 一句 cos=0.85 (候选边, laya 验 w=0.7)
    _setup(monkeypatch, [_zhipu_arr([
        {"id": 0, "summary": "种子句复述 #vA", "label": "fact"},
        {"id": 1, "summary": "近邻句 #vC", "label": "fact"},
    ])], edge=0.7)
    r = distill.distill_segment("复述句。近邻句。", "s1", CWD, "t2")
    assert (r["merged"], r["atoms"], r["edges"]) == (1, 1, 1)
    refs = json.loads(tdb.execute("SELECT source_refs FROM atom WHERE id=?",
                                  (seed_id,)).fetchone()[0])
    assert refs == ["种子句。", "复述句。"]  # 并成员 = source_refs 追加
    row = tdb.execute("SELECT a_id, b_id, w FROM atom_edge").fetchone()
    assert (row["a_id"], row["b_id"], row["w"]) == (seed_id, seed_id + 1, 0.7)


def test_segment_internal_dupe_merges_no_placeholder_row(tdb, monkeypatch):
    # 段内两句互为 cos=1.0 → 后句并入前句, 只插 1 atom、refs 2 条、无孤儿行
    _setup(monkeypatch, [_zhipu_arr([
        {"id": 0, "summary": "句一 #vA", "label": "fact"},
        {"id": 1, "summary": "句二重复 #vA", "label": "fact"},
    ])])
    r = distill.distill_segment("句一。句二。", "s1", CWD, "t")
    assert (r["atoms"], r["merged"]) == (1, 1)
    refs = json.loads(tdb.execute("SELECT source_refs FROM atom").fetchone()[0])
    assert refs == ["句一。", "句二。"]


# ── 毒 JSON ────────────────────────────────────────────────
# ── m2: 配置/断供挂起, 不判毒 (防裸启动静默丢段) ──────────────
def test_missing_zhipu_key_suspends_not_poison(tdb, monkeypatch):
    """ZHIPU_API_KEY 缺失 → ConfigIncomplete (LayaUnavailable 子类, 挂起
    lane) — 不落 distill_seen, 图零写入; 裸启动错判毒段会被 daemon 当
    成功删 spool 文件 (静默丢记忆)。"""
    _setup(monkeypatch, [])
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    with pytest.raises(distill.LayaUnavailable):
        distill.distill_segment("句子一。", "s1", CWD, "t")
    assert tdb.execute("SELECT COUNT(*) c FROM distill_seen").fetchone()["c"] == 0
    assert tdb.execute("SELECT COUNT(*) c FROM atom").fetchone()["c"] == 0


def test_zhipu_all_network_fail_suspends_not_poison(tdb, monkeypatch):
    """zhipu 3 轮全网络层异常 (有 key 但不可达) → 挂起非毒段; 毒段只留给
    「provider 真返回但输出不可解析」的内容性失败。"""

    class DeadZhipu:
        def chat(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.setattr(distill, "_get_zhipu", lambda: DeadZhipu())
    monkeypatch.setenv("ZHIPU_API_KEY", "k")
    _fake_embed(monkeypatch)
    _fake_laya(monkeypatch)
    with pytest.raises(distill.LayaUnavailable):
        distill.distill_segment("句子一。", "s1", CWD, "t")
    assert tdb.execute("SELECT COUNT(*) c FROM distill_seen").fetchone()["c"] == 0
    assert tdb.execute("SELECT COUNT(*) c FROM atom").fetchone()["c"] == 0


# ── M2: 单事务原子入图 ─────────────────────────────────────
def test_txn_atomic_rollback_on_midwrite_failure(tdb, monkeypatch):
    """入图事务中途异常 → rollback 整体回退: 零 atom 残留、distill_seen
    不落行、连接不留活动事务 (db.transaction 的 finally-commit 会提交异常
    路径已执行语句 — 本路径不走它)。"""
    _setup(monkeypatch, [_zhipu_arr([{"id": 0, "summary": "结论 #vA",
                                      "label": "fact"}])])

    def boom(conn, atom_ids, session_id, cwd, ts):
        raise RuntimeError("mid-txn boom")

    monkeypatch.setattr(distill, "_mint_fact_tags", boom)
    with pytest.raises(RuntimeError, match="mid-txn boom"):
        distill.distill_segment("句子一。", "s1", CWD, "t")
    assert tdb.execute("SELECT COUNT(*) c FROM atom").fetchone()["c"] == 0, \
        "原子性: 已执行 INSERT 不得残留"
    assert tdb.execute("SELECT COUNT(*) c FROM distill_seen"
                       ).fetchone()["c"] == 0
    assert not tdb.in_transaction, "异常路径须 rollback, 不留半开事务"


def test_poison_json_retries_then_succeeds(tdb, monkeypatch):
    fake = _setup(monkeypatch, [
        "前置废话 {{{ 未闭合",                       # 数组正则不中 → 裸对象流不中
        "placeholder text",                          # 完全无结构
        _zhipu_arr([{"id": 0, "summary": "第三次成功 #vA", "label": "fact"}]),
    ])
    r = distill.distill_segment("毒段句。", "s1", CWD, "t")
    assert r["atoms"] == 1
    assert len(fake.calls) == 3  # 重试 2 次后第三次解析成功


def test_poison_segment_goes_to_dlq(tdb, monkeypatch):
    fake = _setup(monkeypatch, ["garbage1", "garbage2", "garbage3"])
    r = distill.distill_segment("毒段句甲。毒段句乙。", "s1", CWD, "t")
    assert r["skipped"] == "poison" and r["atoms"] == 0
    assert len(fake.calls) == 3
    seen = tdb.execute("SELECT status FROM distill_seen").fetchone()
    assert seen["status"] == "poison"  # 重放免疫, 不再计费
    r2 = distill.distill_segment("毒段句甲。毒段句乙。", "s1", CWD, "t")
    assert r2["skipped"] == "seen" and len(fake.calls) == 3


# ── 补扫: audit / reembed ──────────────────────────────────
def test_audit_pending_backfills_p_dur(tdb, monkeypatch):
    _setup(monkeypatch, [_zhipu_arr([{"id": 0, "summary": "结论 #vA", "label": "fact"}])],
           malformed_dur=True)  # probabilities 缺 → needs_audit
    distill.distill_segment("句子。", "s1", CWD, "t")
    row = tdb.execute("SELECT needs_audit, p_dur FROM atom").fetchone()
    assert (row["needs_audit"], row["p_dur"]) == (1, 0.0)  # p_dur 列 DEFAULT 0.0
    _fake_laya(monkeypatch, dur=0.9)  # laya 恢复正常 → 补审
    assert distill.audit_pending() == 1
    row = tdb.execute("SELECT needs_audit, p_dur FROM atom").fetchone()
    assert (row["needs_audit"], row["p_dur"]) == (0, 0.9)
    assert distill.audit_pending() == 0  # 清零后幂等


def test_audit_pending_laya_down_raises(tdb, monkeypatch):
    _setup(monkeypatch, [])
    tdb.execute("INSERT INTO atom(text, label, needs_audit) "
                "VALUES('挂账句', 'fact', 1)")
    monkeypatch.setattr(laya_client, "laya_available", lambda: False)
    with pytest.raises(distill.LayaUnavailable):
        distill.audit_pending()
    assert tdb.execute("SELECT needs_audit FROM atom").fetchone()[0] == 1  # 挂账保留


def test_reembed_needing_backfills_vector(tdb, monkeypatch):
    got_calls = []

    def dead_embed(texts):
        return None  # embed 全失败

    _setup(monkeypatch, [_zhipu_arr([{"id": 0, "summary": "结论 #vA", "label": "fact"}])])
    monkeypatch.setattr(embedding, "embed_batch", dead_embed)
    r = distill.distill_segment("句子。", "s1", CWD, "t")
    assert r["atoms"] == 1  # 不挂起
    assert tdb.execute("SELECT needs_embed FROM atom").fetchone()[0] == 1

    def live_embed(texts):  # embed 恢复 (生产: 批写 embeddings.db L2 缓存)
        got_calls.append(list(texts))
        return [_onehot(2) for _ in texts]

    monkeypatch.setattr(embedding, "embed_batch", live_embed)
    assert distill.reembed_needing() == 1
    assert got_calls and got_calls[0] == ["结论 #vA"]  # 按文本补 (缓存键)
    assert tdb.execute("SELECT needs_embed FROM atom").fetchone()[0] == 0
    assert distill.reembed_needing() == 0
    # embed 仍坏 → passive 0, 挂账保留
    monkeypatch.setattr(embedding, "embed_batch", dead_embed)
    tdb.execute("INSERT INTO atom(text, label, needs_embed) "
                "VALUES('缺向量句', 'fact', 1)")
    assert distill.reembed_needing() == 0
    assert tdb.execute("SELECT COUNT(*) c FROM atom WHERE needs_embed=1"
                       ).fetchone()["c"] == 1
