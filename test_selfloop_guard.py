"""D-B c 图不变量防线 (P4 D-A 升级): 解析后同 id → 否决合并而非丢边。

根因 (生产实测 7 条自环): 边卫生门的表面串检查 (subject == value) 挡不住
resolver 合并 — 两个**不同表面名** (omp / @oh-my-pi/pi-coding-agent) 经
别名/语义合并解析到同一实体 id → A --pred--> A 自环。

D-A 旧语义: 同 id 边整条丢弃 — 病征连着事实一起扔 (真事实 "omp based_on
上游" 不在 KG)。D-B c 新语义: object 名带 exclude_ids={subject_id} 重解析
(resolver step1 命中被拒 / step2 候选滤掉 / 排光新建) → 宁分离勿自环,
边落库到独立实体, 事实保留。

测试规范: def test_xxx() 函数让 pytest 收集; conftest pin regex 档 → 本测
显式覆盖 MEM_EXTRACT_CHANNEL=llm (fixture 恢复, 不泄漏)。"""
import json
import os
import tempfile
from pathlib import Path

import pytest

import autodream
import db
import llm_extract
import store
from llm_provider import EdgeOut, EntityOut, Extraction

SEG_TEXT = "omp 基于 @oh-my-pi/pi-coding-agent 开发, 是用户自己的派生项目"


@pytest.fixture
def llm_channel_env():
    """conftest pin regex → 本测显式覆盖 llm, 用完恢复 (不泄漏其他测试)。"""
    orig = os.environ.get("MEM_EXTRACT_CHANNEL")
    os.environ["MEM_EXTRACT_CHANNEL"] = "llm"
    yield
    if orig is None:
        os.environ.pop("MEM_EXTRACT_CHANNEL", None)
    else:
        os.environ["MEM_EXTRACT_CHANNEL"] = orig


@pytest.fixture
def fresh_db(tmp_path):
    db.init(tmp_path / "mem.db")
    return db.get_conn()


@pytest.fixture
def transcript(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps(
        {"type": "user", "message": {"content": SEG_TEXT}},
        ensure_ascii=False) + "\n", encoding="utf-8")
    return str(t)


def _fake_extract(text, providers=None):
    return Extraction(
        entities=[EntityOut("omp", "identifier"),
                  EntityOut("@oh-my-pi/pi-coding-agent", "package")],
        edges=[EdgeOut("omp", "based_on", "@oh-my-pi/pi-coding-agent")],
        confidence=0.9, source_meta={"provider": "fake", "extractor_label": "llm"})


def test_same_id_resolution_splits_not_drops(llm_channel_env, fresh_db, tmp_path,
                                             transcript):
    """双表面名解析到同一实体 id → 分离: object 独立实体落库, 边保留无自环。

    模拟生产实案: 批解析 (无边约束) 把两个名字都给 omp 的 id — 真实场景是
    resolver 误并; 此处直接用受污染实体表复现同一终态。"""
    # 预置受污染实体: omp 已吸收 @oh-my-pi/pi-coding-agent 作别名 (生产实态)
    omp_id = store.put_entity("omp", "identifier")
    store.add_aliases(omp_id, ["@oh-my-pi/pi-coding-agent"])

    import resolver as resolver_mod
    import embedding
    _orig = llm_extract.extract
    _orig_emb = embedding.embed
    _orig_eb = embedding.embed_batch
    # embedding → [] : resolver step2 跳过 (providers=[] 也跳), 走 step1/step3
    embedding.embed = lambda *a, **kw: []
    embedding.embed_batch = lambda *a, **kw: {}
    llm_extract.extract = _fake_extract
    try:
        r = autodream.autodream(session_id="db-c-split",
                                transcript_path=transcript, providers=[])
    finally:
        llm_extract.extract = _orig
        embedding.embed = _orig_emb
        embedding.embed_batch = _orig_eb

    conn = fresh_db
    loops = conn.execute(
        "SELECT COUNT(*) FROM fact WHERE subject_id = object_id").fetchone()[0]
    assert loops == 0, f"自环必须 0, got {loops}"
    # c 语义: 边保留 — object 分离为独立实体, fact 落库
    facts = conn.execute(
        "SELECT f.subject_id, f.object_id FROM fact f").fetchall()
    assert len(facts) == 1, f"分离后边应落库 1 条 (非丢弃), got {len(facts)} (r={r})"
    s, o = facts[0]["subject_id"], facts[0]["object_id"]
    assert s == omp_id, "subject 应命中既有 omp"
    assert o is not None and o != omp_id, "object 应分离为独立实体"
    # 分离出的实体存在且名字是 surface form
    sep = store.get_entity(o)
    assert sep is not None and sep["name"] == "@oh-my-pi/pi-coding-agent"


def test_identical_surface_edge_still_dropped(llm_channel_env, fresh_db, tmp_path,
                                              transcript):
    """真同串 (subject == object 表面) → 边本身语义无效, 丢弃 (D-A 保留路径)。"""
    def _selfedge_extract(text, providers=None):
        return Extraction(
            entities=[EntityOut("omp", "identifier")],
            edges=[EdgeOut("omp", "based_on", "omp")],
            confidence=0.9,
            source_meta={"provider": "fake", "extractor_label": "llm"})

    import embedding
    _orig = llm_extract.extract
    _orig_emb = embedding.embed
    _orig_eb = embedding.embed_batch
    embedding.embed = lambda *a, **kw: []
    embedding.embed_batch = lambda *a, **kw: {}
    llm_extract.extract = _selfedge_extract
    try:
        autodream.autodream(session_id="db-c-samestr",
                            transcript_path=transcript, providers=[])
    finally:
        llm_extract.extract = _orig
        embedding.embed = _orig_emb
        embedding.embed_batch = _orig_eb

    total = fresh_db.execute("SELECT COUNT(*) FROM fact").fetchone()[0]
    loops = fresh_db.execute(
        "SELECT COUNT(*) FROM fact WHERE subject_id = object_id").fetchone()[0]
    assert total == 0 and loops == 0, f"A-->A 同串边应丢弃, got {total}/{loops}"
