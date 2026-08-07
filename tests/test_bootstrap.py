"""Node M — KG init bootstrap from CC memory .md (ADR-12).

Drives ``bootstrap.init_memory(memory_dir, providers, fact_type='permanent')``
against an isolated per-test SQLite, covering the import path (ADD permanent
fact via mock LLM), idempotency (re-run ⇒ NOOP), missing-dir tolerance, and
multi-file accumulation. Reuses autodream's 增量决策 — no logic duplicated here.
"""

from __future__ import annotations

import os
import sys

import pytest

_SRV_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRV_DIR not in sys.path:
    sys.path.insert(0, _SRV_DIR)

import bootstrap  # noqa: E402
import db  # noqa: E402
import store  # noqa: E402
from conftest import RegexMockProvider  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path):
    """Per-test isolated SQLite file; resets db's cached connection."""
    db.init(str(tmp_path / "memory.db"))
    yield tmp_path


def _write_md(tmp_path, name, text):
    """Write one .md into a memory subdir, return the dir path."""
    d = tmp_path / "mem"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")
    return str(d)


class _FakeLLMProvider:
    """Deterministic LLM stand-in: always surfaces one fixed fact regardless
    of input. Exercises the bootstrap→autodream→adapter LLM path without a
    real CCR call. _is_reachable: no base_url → extract_facts("") probe
    (returns the fixed fact, no stub sentinel) → reachable."""

    def extract_facts(self, text):
        from llm_provider import Extraction, FactOut
        return Extraction(
            facts=[FactOut("Alice", "uses", "Python")],
            confidence=0.7,
            source_meta={"provider": "fake-llm"},
        )


# ── import path (ADD permanent fact) ────────────────────────────────

def test_init_memory_imports_permanent_fact(fresh_db):
    """ADR-12: init_memory(dir, [mock]) → ADD fact with fact_type='permanent'
    and extractor='llm' (mock LLM path)."""
    d = _write_md(fresh_db, "a.md", "任何内容, mock 不看")
    r = bootstrap.init_memory(d, providers=[_FakeLLMProvider()])
    assert r["files"] == 1, r
    assert r["added"] >= 1, r
    subj = store.find_entities_by_name("Alice")
    assert subj, "LLM-extracted subject entity created"
    facts = store.get_facts_by_subject(subj[0]["id"], status="active")
    the_fact = next(f for f in facts if f["predicate"] == "uses" and f["value"] == "Python")
    assert the_fact["fact_type"] == "permanent", the_fact
    assert the_fact["extractor"] == "llm", the_fact


# ── idempotency (acceptance contract) ───────────────────────────────

def test_init_memory_idempotent(fresh_db):
    """ADR-12: re-run init_memory on the same dir → added==0 (NOOP/UPDATE,
    autodream 增量决策). Safe to re-run after editing memory files."""
    d = _write_md(fresh_db, "a.md", "任何内容, mock 不看")
    r1 = bootstrap.init_memory(d, providers=[_FakeLLMProvider()])
    r2 = bootstrap.init_memory(d, providers=[_FakeLLMProvider()])
    assert r1["added"] >= 1, r1
    assert r2["added"] == 0, r2


# ── robustness ──────────────────────────────────────────────────────

def test_init_memory_missing_dir_noop(fresh_db):
    """Non-existent dir → all-zero (no crash), reports skipped path."""
    r = bootstrap.init_memory(str(fresh_db / "nope"), providers=[])
    assert r["files"] == 0 and r["added"] == 0, r
    assert "skipped" in r, r


def test_init_memory_multiple_files_accumulate(fresh_db):
    """Multiple .md files each scanned; files==2 + at least one fact.

    ``added >= 1``(not ==2): regex 中文盲区(ADR-11)对 '用户使用 rust' 抽取不稳,
    'Bob uses Java.' 确定; 主 LLM 路径在 test_init_memory_imports_permanent_fact
    已验。这里测多文件扫描累加(files==2), 非抽取数。"""
    d = fresh_db / "mem"
    d.mkdir()
    (d / "a.md").write_text("用户使用 rust", encoding="utf-8")
    (d / "b.md").write_text("Bob uses Java.", encoding="utf-8")
    r = bootstrap.init_memory(str(d), providers=[RegexMockProvider()])  # mock LLM (deterministic)
    assert r["files"] == 2, r
    assert r["added"] >= 1, r
