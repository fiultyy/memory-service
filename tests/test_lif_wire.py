"""Node D — LIF-Wire regression: recall boost (ADR-8v2 reinforcement) +
consolidate refactor (decay folded into recency, _merge_group per-dim max).

Pins the three contracts the node wired:

1. ``recall.recall`` boosts hit facts: ``access_count``/``last_accessed_at``
   advance, ``lif_freq``/``lif_recency`` rise, stored ``LIF`` reflects the
   recompute. ``boost=False`` is a pure read (no advance).
2. ``consolidate.decay`` recomputes all five dims via ``compute_lif`` — the
   ADR-8 scalar decay is gone; recency now drives decay (older ``created_at``
   ⇒ lower ``lif_recency``). ``original_lif`` is no longer the decay base.
3. ``consolidate._merge_group`` absorbs the max of each LIF dim across the
   group and recomputes ``LIF`` from ``LIF_WEIGHTS`` (not ``compute_lif``).
"""

from __future__ import annotations

import os
import sys

import pytest

_SRV_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRV_DIR not in sys.path:
    sys.path.insert(0, _SRV_DIR)

import consolidate  # noqa: E402
import db  # noqa: E402
import recall  # noqa: E402
import scoring  # noqa: E402
import store  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path):
    db.init(str(tmp_path / "memory.db"))
    yield


# ── recall boost ────────────────────────────────────────────────────────

def test_recall_boost_advances_access_stats(fresh_db):
    """A hit fact's access_count/last_accessed_at advance on recall and LIF
    reflects the recompute (freq rises from 0 toward saturation)."""
    eid = store.put_entity("用户", "user")
    fid = store.put_fact(eid, "uses", "rust")
    before = store.get_fact(fid)
    assert int(before["access_count"]) == 0
    assert before["lif_freq"] == 0.0

    hits = recall.recall("rust", boost=True)
    assert any(h["id"] == fid for h in hits)
    after = store.get_fact(fid)
    assert int(after["access_count"]) == 1, after["access_count"]
    assert after["last_accessed_at"] is not None
    # freq = 1 - exp(-1/5) ≈ 0.181 > 0 — saturation started.
    assert after["lif_freq"] > 0.0, after["lif_freq"]
    # Returned fact carries the reinforced LIF (not the pre-recall snapshot).
    assert abs(hits[0]["LIF"] - after["LIF"]) < 1e-12


def test_recall_boost_false_is_pure_read(fresh_db):
    """boost=False leaves access_count frozen — pure read, no reinforcement."""
    eid = store.put_entity("用户", "user")
    fid = store.put_fact(eid, "uses", "rust")
    recall.recall("rust", boost=False)
    after = store.get_fact(fid)
    assert int(after["access_count"]) == 0, after["access_count"]


def test_recall_boost_session_id_absorbed_into_spread(fresh_db):
    """session_id flows into seen_sessions ⇒ distinct_sessions ⇒ lif_spread."""
    eid = store.put_entity("用户", "user")
    fid = store.put_fact(eid, "uses", "rust")
    recall.recall("rust", session_id="sess-A")
    after = store.get_fact(fid)
    assert "sess-A" in after["seen_sessions"], after["seen_sessions"]
    # spread = min(1, 1/5) = 0.2
    assert abs(after["lif_spread"] - 0.2) < 1e-9, after["lif_spread"]


def test_recall_boost_idempotent_within_session(fresh_db):
    """Re-recalling in the same session does not double-count the session in
    seen_sessions (spread idempotent), but access_count/last_accessed_at advance
    each call (the reinforcement signal)."""
    eid = store.put_entity("用户", "user")
    fid = store.put_fact(eid, "uses", "rust")
    recall.recall("rust", session_id="sess-A")
    recall.recall("rust", session_id="sess-A")
    after = store.get_fact(fid)
    assert after["seen_sessions"] == ["sess-A"], after["seen_sessions"]
    assert int(after["access_count"]) == 2, after["access_count"]


# ── consolidate.decay (recency absorbs decay) ───────────────────────────

def test_decay_recomputes_recency_from_created_at(fresh_db):
    """A freshly-stored stable fact has recency near 1.0 (age_h ≈ 0). The
    recompute writes lif_recency/LIF; the ADR-8 scalar decay base
    (original_lif*0.5**(Δt/h)) is gone — recency drives decay now."""
    eid = store.put_entity("用户", "user")
    fid = store.put_fact(eid, "uses", "rust", fact_type="stable")
    out = consolidate.decay()
    f = store.get_fact(fid)
    # age_h ≈ 0 ⇒ recency = exp(0) = 1.0 (within clock jitter).
    assert f["lif_recency"] > 0.999, f["lif_recency"]
    assert out["decayed"] >= 1  # the new dims differ from the 0/0.5 store defaults


def test_decay_permanent_fact_recency_one(fresh_db):
    """permanent fact_type ⇒ half_life = ∞ ⇒ recency = 1.0 (never decays)."""
    eid = store.put_entity("用户", "user")
    fid = store.put_fact(eid, "uses", "rust", fact_type="permanent")
    consolidate.decay()
    f = store.get_fact(fid)
    assert abs(f["lif_recency"] - 1.0) < 1e-9, f["lif_recency"]


def test_decay_idempotent_back_to_back(fresh_db):
    """ADR-8v2 idempotency: a second decay() immediately after the first
    short-circuits (same wall-clock second + ms-floor ⇒ same dims ⇒ no write).
    Pins the ms-floor + 1e-9 tolerance argument with an automated regression."""
    eid = store.put_entity("用户", "user")
    store.put_fact(eid, "uses", "rust", fact_type="stable")
    consolidate.decay()  # primes stored dims from store defaults
    second = consolidate.decay()
    assert second["decayed"] == 0, second


# ── consolidate._merge_group (per-dim max + LIF recompute) ──────────────

def test_merge_group_absorbs_per_dim_max_and_recomputes_lif(fresh_db):
    """Two duplicate facts (same subject/predicate/object_key) with different
    dim values: survivor absorbs the max of each dim, LIF is recomputed from
    LIF_WEIGHTS (not max-of-LIF)."""
    eid = store.put_entity("用户", "user")
    # group is keyed by (subject_id, predicate, object_key); same value "rust".
    f1 = store.put_fact(
        eid, "uses", value="rust", extractor="regex",  # source=0.4
        lif_freq=0.5, lif_recency=0.5, lif_spread=0.2, lif_coherence=1.0,
    )
    f2 = store.put_fact(
        eid, "uses", value="rust", extractor="llm",  # source=0.7
        lif_freq=0.9, lif_recency=0.3, lif_spread=0.6, lif_coherence=0.8,
    )
    group = list(consolidate._group_duplicate_facts(db.get_conn()).values())[0]
    n = consolidate._merge_group(group)
    assert n == 1

    survivor_row = db.get_conn().execute(
        "SELECT * FROM fact WHERE status = 'active' AND subject_id = ?", (eid,)
    ).fetchone()
    # max per dim: freq=0.9, recency=0.5, spread=0.6, coherence=1.0, source=0.7
    assert abs(float(survivor_row["lif_freq"]) - 0.9) < 1e-9
    assert abs(float(survivor_row["lif_recency"]) - 0.5) < 1e-9
    assert abs(float(survivor_row["lif_spread"]) - 0.6) < 1e-9
    assert abs(float(survivor_row["lif_coherence"]) - 1.0) < 1e-9
    assert abs(float(survivor_row["lif_source"]) - 0.7) < 1e-9
    w = scoring.LIF_WEIGHTS
    expect_lif = (
        w["freq"] * 0.9 + w["recency"] * 0.5 + w["spread"] * 0.6
        + w["coherence"] * 1.0 + w["source"] * 0.7
    )
    assert abs(float(survivor_row["LIF"]) - expect_lif) < 1e-9, (
        survivor_row["LIF"], expect_lif,
    )
