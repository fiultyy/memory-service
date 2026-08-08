#!/usr/bin/env python3
"""验证 bootstrap.py 跳过 source:mem-service 投影 md (ADR-16f)"""

import os
import tempfile
import sys

sys.path.insert(0, os.path.dirname(__file__))

from bootstrap import init_memory
from llm_provider import EdgeOut, EntityOut, Extraction


class FakeProvider:
    """Fake provider 返回固定事实，用于验证是否被 ingest"""
    base_url = None

    def __init__(self, expected_edge, expected_entities=None):
        self.expected_edge = expected_edge
        self.expected_entities = expected_entities or []
        self.seen_texts = []

    def extract_facts(self, text: str):
        self.seen_texts.append(text)
        return Extraction(
            entities=self.expected_entities,
            edges=[self.expected_edge],
            confidence=0.9,
            source_meta={"provider": "fake"}
        )


def test_bootstrap_skips_mem_service_projection():
    """验证:
    1. CC 原生 md (无 source frontmatter) 正常 ingest
    2. 投影 md (source: mem-service) 被跳过，不喂 autodream
    """
    d = tempfile.mkdtemp()

    # CC 原生 md (应该被 ingest)
    native_path = os.path.join(d, "native.md")
    with open(native_path, "w") as f:
        f.write("用户使用 rust")

    # mem-service 投影 md (应该被跳过)
    proj_path = os.path.join(d, "mem-x.md")
    with open(proj_path, "w") as f:
        f.write("---\nsource: mem-service\nfact_id: x\n---\n用户 uses rust")

    # Fake provider: 记录收到的 text，验证 mem-x.md 没有被喂进去
    provider = FakeProvider(
        EdgeOut("用户", "uses", "rust", topic="用户使用 rust"),
        [EntityOut("用户", "person"), EntityOut("rust", "tool")],
    )

    # 隔离 DB (用文件而非目录)
    import db
    db_fd, db_tmp = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    db.init(db_tmp)

    # 执行
    r = init_memory(d, providers=[provider])

    # 验证
    print(f"[INFO] totals: {r}")
    assert r["files"] == 1, f"应处理 1 个文件 (native)，实际: {r['files']}"
    assert r["skipped"] == 1, f"应跳过 1 个文件 (mem-x.md)，实际: {r['skipped']}"

    # 关键: provider 只看到了 native.md 的内容，没看到 mem-x.md 的内容
    # 注意: 按 CHUNK 分段，可能被分多段，但所有段都不应含投影内容
    all_text = "".join(provider.seen_texts)
    assert "用户使用 rust" in all_text, "provider 应收到 native.md 内容"
    assert "用户 uses rust" not in all_text, "provider 不应收到 mem-x.md 内容（投影被跳过）"

    # 验证 KG 中没有来自 mem-x.md 的 fact
    from db import get_conn
    conn = get_conn()
    cur = conn.cursor()

    # 查所有 fact 的 source_refs，确认无 mem-x.md
    cur.execute("SELECT source_refs FROM fact")
    all_refs = []
    for row in cur.fetchall():
        refs = row[0] or "[]"
        all_refs.append(refs)
        if "mem-x.md" in refs:
            raise AssertionError(f"KG 中存在来自 'mem-x.md' 的 fact: {refs}")

    print(f"[INFO] 所有 fact 的 source_refs: {all_refs}")

    # 应至少有一个 fact 的 source_refs 包含 native.md
    has_native = any("native.md" in refs for refs in all_refs)
    assert has_native, f"KG 应包含来自 'native.md' 的 fact，实际 refs: {all_refs}"

    print("[PASS] bootstrap 正确跳过 source:mem-service 投影 md")


if __name__ == "__main__":
    test_bootstrap_skips_mem_service_projection()
