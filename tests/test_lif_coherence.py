"""Node C — LIF coherence regression: refresh_lif_on_recall must include the
fact's own predicate when scanning subject siblings.

Background (ADR-8v2 bug): ``refresh_lif_on_recall`` builds the coherence
neighbor list from subject siblings via ``WHERE subject_id=? AND id!=?`` —
that excluded the fact itself, so contradiction pairs (uses/avoids) where the
fact carries one half and a sibling carries the other never fired
(``_conflicts`` needs both predicates present). The fix prepends the fact's
own predicate to the neighbor list.

This test pins the fix; the acceptance cmd in the orchestrator brief only ran
the case ad-hoc.
"""

from __future__ import annotations

import os
import sys

import pytest

_SRV_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRV_DIR not in sys.path:
    sys.path.insert(0, _SRV_DIR)

import db  # noqa: E402
import scoring  # noqa: E402
import store  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path):
    db.init(str(tmp_path / "memory.db"))
    yield


def test_refresh_lif_self_plus_sibling_contradiction(fresh_db):
    """Self=uses + sibling=avoids on the same subject ⇒ 1 conflict over 2
    neighbors (self included) ⇒ coherence ≤ 0.5. Pre-fix this returned 1.0."""
    eid = store.put_entity("用户", "user")
    f1 = store.put_fact(eid, "uses", "rust")
    store.put_fact(eid, "avoids", "rust")
    r = scoring.refresh_lif_on_recall(f1)
    assert r is not None
    assert r["lif_coherence"] <= 0.5, r["lif_coherence"]


def test_refresh_lif_no_contradiction_high_coherence(fresh_db):
    """Same-subject facts with non-conflicting predicates ⇒ coherence stays
    at 1.0 (no false conflict signal)."""
    eid = store.put_entity("FastAPI", "framework")
    f1 = store.put_fact(eid, "uses", "Pydantic")
    store.put_fact(eid, "supports", "openapi")
    r = scoring.refresh_lif_on_recall(f1)
    assert r is not None
    assert abs(r["lif_coherence"] - 1.0) < 1e-9, r["lif_coherence"]
