"""Node O — 向量召回融合 (ADR-13 阶段 2).

score_fact vec_sim 维 (δ·vec_sim) + recall use_vec 向量候选扩展(解 synonym/rewrite
字面盲区)。Mock embedding(deterministic, 不依赖 LM Studio/Ollama)。
"""

from __future__ import annotations

import os
import sys

import pytest

_SRV_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRV_DIR not in sys.path:
    sys.path.insert(0, _SRV_DIR)

import embedding  # noqa: E402
import recall as recall_mod  # noqa: E402
import scoring  # noqa: E402
import db  # noqa: E402
import store  # noqa: E402


# ── score_fact vec_sim 维 (ADR-13) ───────────────────────────────────

def test_score_fact_vec_sim_dim():
    """vec_sim=0.5, delta=0.3 → score += delta·vec_sim = 0.15 (相对 vec_sim=0)。
    返回 dict 含 vec_sim 字段。"""
    fact = {"value": "rust", "LIF": 0.5}
    s0 = scoring.score_fact(fact, "rust", centrality=0.0, vec_sim=0.0, delta=0.3)
    s1 = scoring.score_fact(fact, "rust", centrality=0.0, vec_sim=0.5, delta=0.3)
    assert abs((s1["score"] - s0["score"]) - 0.3 * 0.5) < 1e-9, (s0["score"], s1["score"])
    assert s1["vec_sim"] == 0.5
    # 默认 delta(DELTA_VEC) 不传 — vec_sim 仍计入
    s2 = scoring.score_fact(fact, "rust", centrality=0.0, vec_sim=1.0)
    assert s2["vec_sim"] == 1.0


def test_score_fact_default_off_unchanged():
    """vec_sim 默认 0.0 → score 与 ADR-4v2(无 vec_sim)一致, 不破现有 score_fact 契约。"""
    fact = {"value": "rust", "LIF": 0.5}
    s = scoring.score_fact(fact, "rust", centrality=0.4, weights=(0.5, 0.3, 0.2))
    # score = 0.5·match + 0.3·0.4 + 0.2·0.5 + delta·0(vec_sim 默认 0)
    expected = 0.5 * scoring.match_item("rust", ["rust"], "rust") + 0.3 * 0.4 + 0.2 * 0.5
    assert abs(s["score"] - expected) < 1e-9, (s["score"], expected)
    assert s["vec_sim"] == 0.0


# ── recall use_vec 向量候选扩展 (ADR-13) ─────────────────────────────

@pytest.fixture()
def fresh_db(tmp_path):
    db.init(str(tmp_path / "memory.db"))
    yield tmp_path


@pytest.fixture(autouse=True)
def _isolated_embed_cache(tmp_path, monkeypatch):
    """每测试隔离 embedding cache(L1 内存 + L2 SQLite)到 tmp, 避免跨测试/生产污染。"""
    import embedding
    monkeypatch.setattr(embedding, "_CACHE_DB", tmp_path / "embeddings.db")
    embedding.clear_cache()
    yield
    embedding.clear_cache()


class _MatchEmbedding:
    """Deterministic embedding stand-in: '铁锈' 与 'rust' embed 相同(cosine 1.0),
    其他 → [0,1,0]。模拟 synonym 语义近, 不依赖 LM Studio。"""

    def embed(self, text):
        return [1.0, 0.0, 0.0] if text in ("铁锈", "rust") else [0.0, 1.0, 0.0]


def test_recall_use_vec_blind_spot(fresh_db, monkeypatch):
    """ADR-13: query '铁锈'(synonym) 字面无候选(entity/value 都不 match '铁锈'),
    use_vec=True 经向量召回(cosine 1.0 >= VEC_MIN) 命中 'rust' fact。"""
    eid = store.put_entity("用户", "inferred")
    store.put_fact(eid, "uses", "rust", extractor="regex")

    monkeypatch.setattr(embedding, "default_providers", lambda: [_MatchEmbedding()])
    embedding.clear_cache()

    # 字面: '铁锈' 无 entity LIKE / value substring match → 盲区(空)
    lit = recall_mod.recall("铁锈", boost=False)
    assert lit == [], f"字面应盲区(无 match): {lit}"

    # 向量: '铁锈'↔'rust' cosine 1.0 → 候选扩展 → 命中
    vec = recall_mod.recall("铁锈", boost=False, use_vec=True)
    assert any(f["value"] == "rust" for f in vec), f"向量应命中 rust: {vec}"


def test_recall_use_vec_off_default(fresh_db, monkeypatch):
    """use_vec 默认 False → 不调 embedding(回退纯字面), 与 ADR-4v2 recall 一致。"""
    eid = store.put_entity("用户", "inferred")
    store.put_fact(eid, "uses", "rust", extractor="regex")

    called = []

    class _SpyEmbedding:
        def embed(self, text):
            called.append(text)
            return [1.0, 0.0]

    monkeypatch.setattr(embedding, "default_providers", lambda: [_SpyEmbedding()])
    embedding.clear_cache()

    # use_vec 默认 False — 'rust' 字面命中, 不调 embedding
    r = recall_mod.recall("rust", boost=False)
    assert any(f["value"] == "rust" for f in r), r
    assert called == [], f"use_vec=False 不应调 embedding: {called}"
