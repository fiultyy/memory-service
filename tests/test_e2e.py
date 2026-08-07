"""Node F — closed-loop e2e: ingest → recall → hit + ordering + schema join.

Drives the top seams end-to-end against an isolated per-test SQLite file:

- ``cli.ingest`` (Node B) extracts entities+facts via the adapter's regex
  fallback path (``providers=[RegexMockProvider()]``, deterministic — ADR-5 upheld; the LLM path
  is exercised in tests/test_adapter.py) and persists them; ``cli.recall``
  (Spec §6 seam) navigates the KG, scores
  ``α·match + β·centrality + γ·LIF`` (ADR-4v2, weighted — supersedes v1
  ``match × lif``) and returns Facts ordered desc.
- Schema cross-table join consistency: every returned Fact's ``subject_id`` /
  ``object_id`` resolves to a row in ``entity`` (FK holds), and Fact columns
  match the ADR-2/3 schema.
- No decay assertions (type-aware LIF decay deferred — ADR-6, task scope).

Acceptance cmd: ``cd services/memory-service && python -m pytest tests/test_e2e.py -q``.
"""

from __future__ import annotations

import os
import sys

import pytest

# Make the service package importable as top-level modules (cli, db, store, ...)
# regardless of pytest's invocation cwd.
_SRV_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRV_DIR not in sys.path:
    sys.path.insert(0, _SRV_DIR)

import cli  # noqa: E402
import db  # noqa: E402
import scoring  # noqa: E402
import store  # noqa: E402
from conftest import RegexMockProvider  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path):
    """Per-test isolated SQLite file; resets db's cached connection.

    db.init() switches the active connection when db_path differs, so each
    test starts from a clean schema with deterministic state.
    """
    db_path = tmp_path / "memory.db"
    db.init(str(db_path))
    yield db_path


# ── helpers ────────────────────────────────────────────────────────────

def _fact_values(facts):
    return [f["value"] for f in facts]


def _assert_schema_join(facts):
    """Every Fact joins back to entity: subject_id always resolves, object_id
    resolves when present. Columns carry the ADR-2/3 contract."""
    for f in facts:
        assert f["status"] == "active"
        # subject_id is NOT NULL in schema (FK → entity.id).
        subj = store.get_entity(f["subject_id"])
        assert subj is not None, f"Fact.subject_id {f['subject_id']} dangling"
        # object_id nullable; when present it must resolve.
        if f["object_id"]:
            obj = store.get_entity(f["object_id"])
            assert obj is not None, f"Fact.object_id {f['object_id']} dangling"
        # ADR-4: LIF is a [0,1] storage scalar (not NeuralField rank).
        assert 0.0 <= f["LIF"] <= 1.0
        # extractor: "llm" (adapter LLM 路径, mock 或真) 或测试 seed 字面。
        assert f["extractor"] in ("llm", "regex")


# ── closed loop: ingest → recall (literal hit) ────────────────────────

def test_ingest_then_recall_literal_hit(fresh_db):
    """User Story 6: ingest '用户使用 rust 进行开发' → recall 'rust' returns
    Fact(subject=用户, predicate=uses, object=rust). Literal/substring hit."""
    summary = cli.ingest("用户使用 rust 进行开发", providers=[RegexMockProvider()])
    assert summary["facts"], "ingest produced no facts"
    assert summary["entities"] >= 1

    hits = cli.recall("rust")
    assert hits, "recall returned no facts for a literal hit"
    assert any(h["value"] == "rust" for h in hits), _fact_values(hits)

    the_fact = next(h for h in hits if h["value"] == "rust")
    subj = store.get_entity(the_fact["subject_id"])
    assert subj["name"] == "用户"
    assert the_fact["predicate"] == "uses"
    _assert_schema_join(hits)


def test_multi_ingest_recall_each_query(fresh_db):
    """Several ingests → each query recalls its own fact (literal hit, no
    cross-talk). The closed loop is the Node F deliverable.

    ADR-4: match is a substring hit on Fact.value (the content carrier), so
    queries target value tokens ('rust'/'Pydantic'/'笔记'), not subject names.
    """
    cli.ingest("用户使用 rust 进行开发", providers=[RegexMockProvider()])  # value='rust'
    cli.ingest("FastAPI uses Pydantic.", providers=[RegexMockProvider()])  # value='Pydantic'
    cli.ingest("Logseq 是笔记工具", providers=[RegexMockProvider()])      # value='笔记工具'

    rust = cli.recall("rust")
    pyd = cli.recall("Pydantic")
    log = cli.recall("笔记")

    assert any("rust" == h["value"] for h in rust), _fact_values(rust)
    assert any("Pydantic" == h["value"] for h in pyd), _fact_values(pyd)
    assert any(h["predicate"] == "is_a" for h in log), log

    # No cross-talk: 'rust' query should not surface the Pydantic/Logseq facts
    # (literal value match is the v1 signal — ADR-4).
    assert "Pydantic" not in _fact_values(rust)

    _assert_schema_join(rust + pyd + log)


# ── ordering: scored = α·match + β·centrality + γ·LIF (ADR-4v2) ────────

def test_recall_orders_by_weighted_fusion(fresh_db):
    """ADR-4v2: ``score = α·match + β·centrality + γ·LIF`` (weighted, supersedes
    v1 ``match × lif``). Verifies the verbose detail carries the centrality field
    and that scores equal the weighted sum, returned sorted desc. cli.ingest
    stamps LIF=0.5; one higher-LIF fact is seeded directly via store."""
    # Entity + a cli-ingested fact (LIF=0.5 default).
    cli.ingest("用户使用 rust 进行开发", providers=[RegexMockProvider()])
    alice = store.put_entity("Alice", "person")
    # Higher-LIF fact whose value also contains 'rust' ⇒ match equal, LIF up.
    store.put_fact(
        alice, "uses", value="rust for backend",
        LIF=0.9, extractor="regex",
    )

    hits = cli.recall("rust")
    assert len(hits) >= 2, [h["value"] for h in hits]

    # Verbose path exposes match/centrality/lif/score (debug surface, Spec §4
    # story 3, ADR-4v2). Centrality is per-entity pagerank (ADR-2v2) — the
    # cli-fact's subject entity is more connected, so β·centrality dominates
    # γ·LIF and the LIF=0.5 fact ranks first.
    detail = cli.recall("rust", verbose=True)
    assert all({"score", "match", "centrality", "lif"} <= set(d) for d in detail)
    scores = [d["score"] for d in detail]
    assert scores == sorted(scores, reverse=True), scores
    # score == α·match + β·centrality + γ·lif (ADR-4v2 literal).
    a, b, g = scoring.ALPHA_MATCH, scoring.BETA_CENTRALITY, scoring.GAMMA_LIF
    for d in detail:
        expect = a * d["match"] + b * d["centrality"] + g * d["lif"]
        assert abs(d["score"] - expect) < 1e-9, (d["score"], expect)
    # Both candidate facts are present.
    assert {d["fact"]["value"] for d in detail} >= {"rust", "rust for backend"}
    _assert_schema_join([d["fact"] for d in detail])


def test_recall_zero_match_and_isolated_centrality_filtered(fresh_db):
    """ADR-4v2: weighted fusion means LIF=0 no longer zeroes score. The
    filter instead drops facts with zero total signal (no match AND a
    disconnected/isolated entity → centrality 0). v1's ``LIF=0 ⇒ dropped``
    is a deliberate ADR-4v2 consequence (supersedes ADR-4 multiplicative)."""
    cli.ingest("用户使用 rust 进行开发", providers=[RegexMockProvider()])  # LIF=0.5 default — survives
    bob = store.put_entity("Bob", "person")
    # LIF=0 but literal-hit 'rust': weighted score = α·1 + β·c + γ·0 > 0 ⇒ kept
    # (ADR-4v2 consequence — LIF=0 no longer forces a drop).
    store.put_fact(bob, "uses", value="rust buried", LIF=0.0, extractor="regex")

    hits = cli.recall("rust")
    assert any(h["value"] == "rust buried" for h in hits), [h["value"] for h in hits]


# ── schema cross-table join consistency ────────────────────────────────

def test_schema_fact_entity_join_consistent(fresh_db):
    """Every Fact returned joins cleanly to entity (FK integrity); the ADR-2/3
    content-carrier columns are present and well-formed. Cross-table
    consistency = Node F scope."""
    cli.ingest("用户使用 rust 进行开发", providers=[RegexMockProvider()])
    cli.ingest("FastAPI uses Pydantic.", providers=[RegexMockProvider()])
    cli.ingest("Logseq 是笔记工具", providers=[RegexMockProvider()])

    # ADR-3 content-carrier + identity columns every Fact must carry.
    required_cols = {
        "id", "subject_id", "predicate", "object_id", "value",
        "LIF", "extractor", "status", "created_at",
    }
    for q in ("rust", "Pydantic", "笔记"):
        for f in cli.recall(q):
            assert required_cols <= set(f.keys()), required_cols - set(f.keys())
            _assert_schema_join([f])
            # Cross-table: subject entity's name is the relation subject.
            subj = store.get_entity(f["subject_id"])
            assert subj["name"] in {"用户", "FastAPI", "Logseq"}


def test_no_memoryitem_table_schema_holds(fresh_db):
    """ADR-2: there is no MemoryItem table — Fact reification is self-contained.
    The only base tables are entity + fact."""
    conn = db.get_conn()
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "entity" in tables and "fact" in tables
    assert "memoryitem" not in tables and "memory_item" not in tables
