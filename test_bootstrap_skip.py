#!/usr/bin/env python3
"""验证 bootstrap.py 跳过 source:mem-service 投影 md (ADR-16f) + 裁决#5b
冷启动零 LLM 档退役 (graph-reform v2, 2026-10-01):
- 投影 md 不喂蒸馏管道; 原生 md 走 distill 一步蒸馏 (v2 唯一写径)。
- distill 外呼不可达 → LayaUnavailable 族穿出 init_memory (挂起等恢复),
  零 atom 落库 — 不再 skip+errors 静默吞、不再产离线记忆。
stub distill (零 LLM/零网络), db.init(tmp) 隔离。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

import db
import bootstrap
from src.distill import LayaUnavailable

import pytest


class _StubDistill:
    def __init__(self):
        self.calls = []  # (text, session_id, cwd, ts)

    def distill_segment(self, text, session_id, cwd, ts):
        self.calls.append((text, session_id, cwd, ts))
        return {"atoms": 1, "edges": 0, "merged": 0, "supersede_proposals": []}


@pytest.fixture
def stub(monkeypatch):
    s = _StubDistill()
    monkeypatch.setattr(bootstrap, "distill_mod", s)
    return s


def _fresh_db(tmp_path):
    db.init(tmp_path / "skip.db")


def test_bootstrap_skips_mem_service_projection(tmp_path, stub):
    """验证:
    1. CC 原生 md (无 source frontmatter) 正常经 distill 径 ingest
    2. 投影 md (source: mem-service) 被跳过, 不喂蒸馏管道
    """
    _fresh_db(tmp_path)
    d = tempfile.mkdtemp()

    # CC 原生 md (应该被 ingest)
    native_path = os.path.join(d, "native.md")
    with open(native_path, "w") as f:
        f.write("用户使用 rust")

    # mem-service 投影 md (应该被跳过)
    proj_path = os.path.join(d, "mem-x.md")
    with open(proj_path, "w") as f:
        f.write("---\nsource: mem-service\nfact_id: x\n---\n用户 uses rust")

    r = bootstrap.init_memory(d)
    print(f"[INFO] totals: {r}")
    assert r["files"] == 1, f"应处理 1 个文件 (native)，实际: {r['files']}"
    assert r["skipped"] == 1, f"应跳过 1 个文件 (mem-x.md)，实际: {r['skipped']}"
    assert r["atoms"] >= 1, r

    # 关键: distill 只看到了 native.md 的内容，没看到 mem-x.md 的内容
    all_text = "".join(c[0] for c in stub.calls)
    assert "用户使用 rust" in all_text, "distill 应收到 native.md 内容"
    assert "用户 uses rust" not in all_text, (
        "distill 不应收到 mem-x.md 内容（投影被跳过）")
    # 溯源: session tag 名与库内存量 session:memory:* 同形
    assert all(c[1] == "memory:native.md" for c in stub.calls), stub.calls

    print("[PASS] bootstrap 正确跳过 source:mem-service 投影 md")


def test_bootstrap_unreachable_suspends(tmp_path, monkeypatch):
    """裁决#5b: 冷启动零 LLM 档退役 — distill 外呼不可用时
    LayaUnavailable 穿出 init_memory (挂起等恢复), 零 atom 落库,
    不再产离线记忆。"""

    class Down:
        def distill_segment(self, *a, **k):
            raise LayaUnavailable("simulated provider outage")

    monkeypatch.setattr(bootstrap, "distill_mod", Down())
    _fresh_db(tmp_path)
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "native.md"), "w") as f:
        f.write("用户使用 rust")
    with pytest.raises(LayaUnavailable):
        bootstrap.init_memory(d)  # 挂起 = 响亮上抛 (不再 skip+errors 静默吞)
    n = db.get_conn().execute("SELECT COUNT(*) FROM atom").fetchone()[0]
    assert n == 0, f"挂起语义: 零离线记忆, got {n} atoms"
    print("[PASS] bootstrap 断供挂起 (LayaUnavailable 穿出, 零 atom)")
