#!/usr/bin/env python3
"""验证 bootstrap.py 跳过 source:mem-service 投影 md (ADR-16f) + 裁决#5b
冷启动零 LLM 档退役 (graph-reform v2 H3b, 2026-10-01):
- 投影 md 不喂提取管道; 原生 md 走 llm 主径 (gazetteer 通道退役后唯一写径)。
- provider 不可达 → ProviderUnreachable 穿出 init_memory (挂起等恢复),
  零 fact 落库 — 不再 skip+errors 静默吞、不再产离线记忆。
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

import db
import llm_extract
from bootstrap import init_memory
from llm_extract import ProviderUnreachable
from llm_provider import EdgeOut, EntityOut, Extraction

import pytest


def _mock_llm_extract(monkeypatch, seen: list[str]):
    """记录型 llm 主径 mock: 可观察面 = llm_extract.extract 收到的段文本。"""
    def _extract(text, provider=None):
        seen.append(text)
        return Extraction(
            entities=[EntityOut("用户", "person"), EntityOut("rust", "tool")],
            edges=[EdgeOut("用户", "uses", "rust", topic="用户使用 rust")],
            confidence=0.9,
            source_meta={"provider": "mock", "extractor_label": "llm"},
        )
    monkeypatch.setattr(llm_extract, "extract", _extract)


def _fresh_db():
    db_fd, db_tmp = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    db.init(db_tmp)


def _offline_embed(monkeypatch):
    import embedding
    monkeypatch.setattr(embedding, "embed", lambda t, providers=None: [])


def test_bootstrap_skips_mem_service_projection(monkeypatch):
    """验证:
    1. CC 原生 md (无 source frontmatter) 正常经 llm 主径 ingest
    2. 投影 md (source: mem-service) 被跳过, 不喂 llm 提取管道
    """
    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", "llm")
    _offline_embed(monkeypatch)
    d = tempfile.mkdtemp()

    # CC 原生 md (应该被 ingest)
    native_path = os.path.join(d, "native.md")
    with open(native_path, "w") as f:
        f.write("用户使用 rust")

    # mem-service 投影 md (应该被跳过)
    proj_path = os.path.join(d, "mem-x.md")
    with open(proj_path, "w") as f:
        f.write("---\nsource: mem-service\nfact_id: x\n---\n用户 uses rust")

    seen_texts: list[str] = []
    _mock_llm_extract(monkeypatch, seen_texts)

    _fresh_db()
    r = init_memory(d)

    print(f"[INFO] totals: {r}")
    assert r["files"] == 1, f"应处理 1 个文件 (native)，实际: {r['files']}"
    assert r["skipped"] == 1, f"应跳过 1 个文件 (mem-x.md)，实际: {r['skipped']}"

    # 关键: llm 主径只看到了 native.md 的内容，没看到 mem-x.md 的内容
    all_text = "".join(seen_texts)
    assert "用户使用 rust" in all_text, "llm 主径应收到 native.md 内容"
    assert "用户 uses rust" not in all_text, (
        "llm 主径不应收到 mem-x.md 内容（投影被跳过）")

    # 验证 KG 中没有来自 mem-x.md 的 fact, 且 native 产出走 llm 档
    conn = db.get_conn()
    rows = conn.execute("SELECT source_refs, extractor FROM fact").fetchall()
    for row in rows:
        refs = row[0] or "[]"
        assert "mem-x.md" not in refs, f"KG 中存在来自 'mem-x.md' 的 fact: {refs}"
    assert rows, "KG 应有 native.md 产出"
    assert all(r[1] == "llm" for r in rows), (
        f"退役后唯一写径是 llm 主径, got extractors {[r[1] for r in rows]}")
    has_native = any("native.md" in (r[0] or "[]") for r in rows)
    assert has_native, f"KG 应包含来自 'native.md' 的 fact，实际 refs: {rows}"

    print("[PASS] bootstrap 正确跳过 source:mem-service 投影 md")


def test_bootstrap_unreachable_suspends(monkeypatch):
    """裁决#5b: 冷启动零 LLM 档退役 — provider 不可达时 ProviderUnreachable
    穿出 init_memory (挂起等恢复), 零 fact 落库, 不再产离线记忆。"""
    monkeypatch.setenv("MEM_EXTRACT_CHANNEL", "llm")
    _offline_embed(monkeypatch)
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "native.md"), "w") as f:
        f.write("用户使用 rust")

    def _boom(text, provider=None):
        raise ProviderUnreachable("simulated provider outage")

    monkeypatch.setattr(llm_extract, "extract", _boom)
    _fresh_db()
    with pytest.raises(ProviderUnreachable):
        init_memory(d)  # 挂起 = 响亮上抛 (不再 skip+errors 静默吞)
    n = db.get_conn().execute("SELECT COUNT(*) FROM fact").fetchone()[0]
    assert n == 0, f"挂起语义: 零离线记忆, got {n} facts"
    print("[PASS] bootstrap 断供挂起 (ProviderUnreachable 穿出, 零 fact)")
