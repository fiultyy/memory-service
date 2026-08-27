"""P4 修: 解析后自环防线 — autodream 边落库前 subject_id == object_id 即弃。

根因 (生产实测 7 条自环): 边卫生门的表面串检查 (subject == value) 挡不住
resolver 合并 — 两个**不同表面名** (omp / @oh-my-pi/pi-coding-agent) 经
别名/语义合并解析到同一实体 id → 落库成 A --pred--> A 自环
(omp based_on omp / Warp part_of Warp / CLAUDE.md located_in CLAUDE.md)。

本测锁: monkeypatch llm_extract.extract 出双名边 + resolver 强制同 id →
autodream 零自环落库。resolver 过并本身 (omp 吸收 @oh-my-pi 别名) 是独立
缺陷, 留 resolver 批次。

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
import resolver
import store
from llm_provider import EdgeOut, EntityOut, Extraction


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


def _transcript(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps(
        {"type": "user", "message": {"content": "omp based on pi coding agent"}},
        ensure_ascii=False) + "\n", encoding="utf-8")
    return str(t)


def _fake_extract(text, providers=None):
    return Extraction(
        entities=[EntityOut("omp", "identifier"),
                  EntityOut("@oh-my-pi/pi-coding-agent", "package")],
        edges=[EdgeOut("omp", "based_on", "@oh-my-pi/pi-coding-agent")],
        confidence=0.9, source_meta={"provider": "fake", "extractor_label": "llm"})


def test_resolution_self_loop_edge_dropped(llm_channel_env, fresh_db, tmp_path):
    """双表面名解析到同一实体 id → 边整条丢弃, 零 fact 落库。"""
    _orig_extract, _orig_batch = llm_extract.extract, resolver.resolve_entities_batch
    llm_extract.extract = _fake_extract
    resolver.resolve_entities_batch = (
        lambda names, **kw: {n: "SAME_ENTITY_ID" for n in names})
    try:
        autodream.autodream(session_id="selfloop-test",
                            transcript_path=_transcript(tmp_path), providers=[])
    finally:
        llm_extract.extract = _orig_extract
        resolver.resolve_entities_batch = _orig_batch

    loops = fresh_db.execute(
        "SELECT COUNT(*) FROM fact WHERE subject_id = object_id").fetchone()[0]
    total = fresh_db.execute("SELECT COUNT(*) FROM fact").fetchone()[0]
    assert loops == 0, f"解析后自环必须 0, got {loops}"
    assert total == 0, f"同 id 解析的边应整条丢弃, got {total} facts"


def test_two_id_resolution_still_lands(llm_channel_env, fresh_db, tmp_path):
    """对照: 双 id 正常解析 → 边落库 (防线不误伤); 假 resolver 真实建实体 (FK)。"""
    _orig_extract, _orig_batch = llm_extract.extract, resolver.resolve_entities_batch

    def _two_ids(names, **kw):
        return {n: store.put_entity(n, "concept") for n in names}

    llm_extract.extract = _fake_extract
    resolver.resolve_entities_batch = _two_ids
    try:
        autodream.autodream(session_id="selfloop-test2",
                            transcript_path=_transcript(tmp_path), providers=[])
    finally:
        llm_extract.extract = _orig_extract
        resolver.resolve_entities_batch = _orig_batch

    total = fresh_db.execute("SELECT COUNT(*) FROM fact").fetchone()[0]
    loops = fresh_db.execute(
        "SELECT COUNT(*) FROM fact WHERE subject_id = object_id").fetchone()[0]
    assert total >= 1, "双 id 正常解析的边应落库 (防线不误伤)"
    assert loops == 0
