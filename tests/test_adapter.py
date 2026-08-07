"""Node Adapter — butterfly-wing LLM extraction (ADR-5b; regex fallback removed).

Paths exercised:

1. **mock provider** (LLM path): a fake ``LLMProvider`` returns facts; the
   adapter votes N=3 wings, aggregates confidence (max), stamps extractor="llm".
2. **block on no provider / all-error**: ``providers=[]`` or every wing errors
   out → ``RuntimeError`` (regex fallback removed — LLM unavailable blocks).
3. **legitimate empty**: wings return no facts with no error → returned empty
   (not a block; the LLM judged the text holds no fact).

Plus a **live** test against ZhipuAnthropicProvider (直连智谱), ``skipif`` the
provider has no key / is unreachable.

Acceptance cmd: ``cd services/memory-service && python -m pytest tests/test_adapter.py -q``.
"""
from __future__ import annotations

import os
import sys

import pytest

_SRV_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRV_DIR not in sys.path:
    sys.path.insert(0, _SRV_DIR)

import adapter  # noqa: E402
import cli  # noqa: E402
import db  # noqa: E402
import store  # noqa: E402
from llm_provider import Extraction, FactOut, ZhipuAnthropicProvider  # noqa: E402


# ── fixtures ─────────────────────────────────────────────────────────

@pytest.fixture()
def fresh_db(tmp_path):
    """Per-test isolated SQLite file; resets db's cached connection."""
    db_path = tmp_path / "memory.db"
    db.init(str(db_path))
    yield db_path


class _FakeProvider:
    """Fake LLMProvider: always returns the same facts at a set confidence.

    Implements the LLMProvider Protocol structurally (duck-typed; no inherit).
    """
    def __init__(self, facts: list[FactOut], confidence: float = 0.9):
        self._facts = facts
        self._confidence = confidence

    def extract_facts(self, text: str) -> Extraction:
        return Extraction(
            facts=list(self._facts), confidence=self._confidence,
            source_meta={"provider": "fake"})


# ── block on no reachable provider (regex fallback removed) ─────────

def test_extract_facts_no_provider_raises():
    """providers=[] → RuntimeError (block). Regex fallback removed — LLM
    unavailable surfaces as an error, not silent low-quality facts."""
    with pytest.raises(RuntimeError, match="no reachable LLM provider"):
        adapter.extract_facts("用户使用 rust", providers=[])


def test_extract_facts_all_providers_error_raises():
    """Every wing errors (no facts, error in source_meta) → RuntimeError
    (block — LLM unavailable, no silent fallback)."""
    class _ErrProvider:
        base_url = None
        def extract_facts(self, text):
            return Extraction(confidence=0.0, source_meta={"error": "boom"})
    with pytest.raises(RuntimeError, match="no facts"):
        adapter.extract_facts("x", providers=[_ErrProvider()], wings=3)


def test_extract_facts_legitimately_empty_no_error_no_raise():
    """Wings return empty facts with NO error → the LLM judged no fact →
    returned empty (not a block; a legitimate empty vote)."""
    provider = _FakeProvider([], confidence=0.0)
    r = adapter.extract_facts("今天天气真不错", providers=[provider], wings=3)
    assert r.facts == []
    assert r.confidence == 0.0


# ── LLM vote path (mock provider) ────────────────────────────────────

def test_extract_facts_llm_majority_vote_aggregates_max_confidence():
    """N=3 wings of a mock provider returning the same fact: the fact survives
    quorum (≥2/3), confidence = max wing confidence."""
    fact = FactOut(subject="用户", predicate="uses", object="实证方法")
    provider = _FakeProvider([fact], confidence=0.9)
    r = adapter.extract_facts("用户偏好实证", providers=[provider], wings=3)
    assert r.facts, "voted LLM fact should survive"
    assert (r.facts[0].subject, r.facts[0].predicate, r.facts[0].object) == (
        "用户", "uses", "实证方法")
    assert r.confidence == 0.9  # max aggregation
    assert r.source_meta.get("wings") == 3
    assert r.source_meta.get("mode") == "majority"


def test_extract_facts_quorum_drops_minority_triples():
    """A triple appearing in only 1 of 3 wings (< quorum 2) is dropped; the
    voted result is empty. No regex fallback — empty vote without errors is
    returned as-is (the LMs legitimately disagreed)."""
    p1 = _FakeProvider([FactOut("a", "uses", "b")], 0.9)
    p2 = _FakeProvider([FactOut("c", "uses", "d")], 0.9)
    p3 = _FakeProvider([FactOut("e", "uses", "f")], 0.9)
    r = adapter.extract_facts("用户使用 rust", providers=[p1, p2, p3], wings=3)
    assert r.facts == []  # no triple reached quorum
    assert r.source_meta.get("mode") == "majority"  # voted, not regex


# ── cli wiring ───────────────────────────────────────────────────────

def test_cli_ingest_llm_path_stamps_extractor_llm(fresh_db):
    """cli.ingest with a high-confidence mock provider stamps extractor="llm"."""
    fact = FactOut(subject="用户", predicate="uses", object="实证方法")
    provider = _FakeProvider([fact], confidence=0.9)
    summary = cli.ingest("用户偏好实证", providers=[provider])
    assert summary["facts"]
    for fid in summary["facts"]:
        assert store.get_fact(fid)["extractor"] == "llm"
    assert store.find_entities_by_name("用户")


def test_cli_ingest_default_providers_uses_mock(fresh_db):
    """cli.ingest with no providers → adapter.default_providers() (autouse-
    patched to RegexMockProvider in tests; ZhipuAnthropicProvider in prod).
    Must persist facts for regex-hittable text (mock extracts it)."""
    summary = cli.ingest("用户使用 rust")
    assert summary["facts"]


def test_cli_ingest_default_block_when_all_unreachable(fresh_db, monkeypatch):
    """In prod default_providers = [ZhipuAnthropicProvider]; if Zhipu is
    unreachable, cli.ingest() raises (block, no regex fallback)."""
    monkeypatch.setattr(adapter, "default_providers", lambda: [])
    with pytest.raises(RuntimeError):
        cli.ingest("用户使用 rust")


# ── ZhipuAnthropicProvider live (skipif no key / unreachable) ────────

def _zhipu_usable() -> bool:
    """True if ZhipuAnthropicProvider resolves a key (env ZHIPU_API_KEY or CCR
    config) AND open.bigmodel.cn is TCP-reachable."""
    from llm_provider import _load_zhipu_key
    if not _load_zhipu_key():
        return False
    import socket
    import urllib.parse
    host = urllib.parse.urlparse(ZhipuAnthropicProvider.base_url).hostname
    try:
        with socket.create_connection((host, 443), timeout=2):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _zhipu_usable(), reason="ZhipuAnthropicProvider no key / unreachable")
def test_zhipu_provider_live_extraction():
    """Live: ZhipuAnthropicProvider 直连智谱 extracts a fact. Contract only."""
    r = ZhipuAnthropicProvider().extract_facts("项目使用 rust 做后端开发")
    assert isinstance(r, Extraction)
    assert 0.0 <= r.confidence <= 1.0
    for f in r.facts:
        assert f.subject and f.predicate and f.object


@pytest.mark.skipif(not _zhipu_usable(), reason="ZhipuAnthropicProvider no key / unreachable")
def test_adapter_zhipu_butterfly_wing_live():
    """Live: adapter.extract_facts with [ZhipuAnthropicProvider()] runs the
    N=3 wing fan-out and returns a voted Extraction."""
    r = adapter.extract_facts(
        "项目使用 rust 做后端开发", providers=[ZhipuAnthropicProvider()], wings=3)
    assert isinstance(r, Extraction)
    assert r.confidence >= 0.0
