"""Shared test fixtures + deterministic LLM mock for mem-service tests.

Production regex *fallback* was removed (adapter raises on no reachable
provider); these tests drive the LLM path deterministically via
``RegexMockProvider`` (regex extractor wrapped as an LLMProvider — same
coverage, no network). ``default_providers`` is autouse-patched to return it
so cli/autodream/bootstrap with ``providers=None`` stay offline-deterministic.
"""
import os
import sys

_SRV_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRV_DIR not in sys.path:
    sys.path.insert(0, _SRV_DIR)

import pytest  # noqa: E402

import adapter  # noqa: E402
import extractor as regex_extractor  # noqa: E402
from llm_provider import Extraction, FactOut  # noqa: E402


class RegexMockProvider:
    """LLMProvider mock wrapping the regex extractor (deterministic, offline).

    Production regex *fallback* is gone; this is a test-only stand-in that
    drives the adapter LLM-vote path without a real Zhipu call. base_url=None
    ⇒ _is_reachable probes extract_facts("") (no stub sentinel) ⇒ reachable.
    """

    base_url = None

    def extract_facts(self, text: str) -> Extraction:
        ex = regex_extractor.extract(text)
        facts = [FactOut(f["subject"], f["predicate"], f["object"])
                 for f in ex["facts"]]
        return Extraction(
            facts=facts, confidence=0.7 if facts else 0.0,
            source_meta={"provider": "regex-mock"})


@pytest.fixture(autouse=True)
def _mock_default_providers(monkeypatch):
    """Patch adapter.default_providers → [RegexMockProvider()] for all tests.

    cli.ingest / autodream / bootstrap with providers=None resolve to the mock
    (offline, deterministic). Tests needing a specific fake pass providers
    explicitly and bypass this.
    """
    monkeypatch.setattr(adapter, "default_providers",
                        lambda: [RegexMockProvider()])


class _NoVecProvider:
    """测试默认禁向量 (store.put_fact 下沉 embed 后, 防真发网络 LM Studio/Ollama)。
    embed 返 [] → embedding.embed passive → cache 不写, put_fact 不崩。"""
    base_url = None
    model = "no-vec-test"
    def embed(self, text: str) -> list:
        return []


@pytest.fixture(autouse=True)
def _mock_embedding(monkeypatch):
    """Patch embedding.default_providers → [_NoVecProvider()] for all tests.
    显式测向量的 test (test_vec_recall) 自己 monkeypatch 覆盖此 patch。"""
    import embedding
    monkeypatch.setattr(embedding, "default_providers", lambda: [_NoVecProvider()])
