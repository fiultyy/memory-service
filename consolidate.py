"""mem-service consolidate — LIF recompute pass + dedup (ADR-8v2 + ADR-6 dedup).

Two phases per ``consolidate()`` call (Spec §2: recompute then dedup):

1. **LIF recompute** (ADR-8v2, idempotent): each active Fact's five LIF dims
   are recomputed via :func:`scoring.compute_lif` and ``LIF`` + the five dim
   columns are written back. ADR-8's scalar decay (``original_lif *
   0.5**(Δt/half_life)``) is folded into the recency dim — ``age_h = now -
   last_accessed_at`` with a ``created_at`` fallback (the original ADR-8
   first-access semantics); ``created_at`` is immutable so the same wall clock
   yields the same dims on every call (no compounding) until a recall refresh
   moves ``last_accessed_at``. Coherence is recomputed authoritatively here
   (recall only refreshes freq/recency/spread cheaply). Facts whose composite
   LIF drops below 0.1 flip ``active → deprecated``.
2. **dedup** (ADR-6 + ADR-8v2 per-dim merge): collapse Facts sharing the same
   (subject_id, predicate, object_key); survivor absorbs the max of each LIF
   dim across the group + the union of source_refs, then ``LIF`` is recomputed
   from the absorbed dims; the rest flip to ``superseded`` pointing at it.

``original_lif`` (ADR-8v2) lives on the fact table; its semantics shifted from
ADR-8 decay base to the source-dim initial-value snapshot — used as the source
fallback only for legacy rows without an extractor. Legacy DBs lacking the
column are backfilled to ``LIF`` by :func:`_ensure_schema` (idempotent ALTER).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import db
import scoring
import store

# ADR-8v2: decay folded into the recency dim, half-life table now lives on
# scoring.LIF_HALF_LIFE_DAYS (the LIF-Scorer canonical home). Re-exported here
# for any caller that historically read consolidate.HALF_LIFE_DAYS.
HALF_LIFE_DAYS: dict[str, float] = scoring.LIF_HALF_LIFE_DAYS

# ADR-8v2: composite LIF below this threshold ⇒ active → deprecated.
DEPRECATE_LIF_THRESHOLD = 0.1

# ADR-8v2 source-dim weight by extractor. Canonical home is scoring.py (the
# LIF-Scorer node); re-exported here so store.put_fact and _ensure_schema's
# legacy backfill keep a single source of truth.
SOURCE_WEIGHT: dict[str, float] = scoring.SOURCE_WEIGHT

_schema_migrated = False


def _ensure_schema() -> None:
    """Idempotent schema migration: back-fill ADR-8 ``original_lif`` and the
    ADR-8v2 LIF five-dim columns for legacy fact tables.

    schema.sql adds the columns on fresh DBs, but ``CREATE TABLE IF NOT
    EXISTS`` skips existing tables — so pre-ADR-8/8v2 DBs need ALTERs here.

    Backfill (ADR-8v2): ``lif_source = SOURCE_WEIGHT[extractor]`` (regex=0.4
    default for unknown extractors), ``lif_recency = 0.5`` (mid-neutral, the
    decay pass recomputes from last_accessed_at=created_at on first run); all
    other new dims default 0 and ``original_lif`` semantics shifts from decay
    base to the source-dim initial-value snapshot.
    """
    global _schema_migrated
    if _schema_migrated:
        return
    conn = db.get_conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fact)").fetchall()}

    # ADR-8 idempotency column (legacy backfill only).
    if "original_lif" not in cols:
        conn.execute("ALTER TABLE fact ADD COLUMN original_lif REAL NOT NULL DEFAULT 0.5")
        conn.execute("UPDATE fact SET original_lif = LIF")

    # ADR-8v2 LIF five-dim + recall-reinforcement state.
    if "lif_freq" not in cols:
        conn.execute("ALTER TABLE fact ADD COLUMN lif_freq REAL NOT NULL DEFAULT 0")
    if "lif_recency" not in cols:
        conn.execute("ALTER TABLE fact ADD COLUMN lif_recency REAL NOT NULL DEFAULT 0.5")
    if "lif_spread" not in cols:
        conn.execute("ALTER TABLE fact ADD COLUMN lif_spread REAL NOT NULL DEFAULT 0")
    if "lif_coherence" not in cols:
        conn.execute("ALTER TABLE fact ADD COLUMN lif_coherence REAL NOT NULL DEFAULT 0")
    if "lif_source" not in cols:
        # Backfill source-dim from extractor (regex=0.4 fallback for unknown).
        conn.execute("ALTER TABLE fact ADD COLUMN lif_source REAL NOT NULL DEFAULT 0.4")
        for ext, w in SOURCE_WEIGHT.items():
            conn.execute("UPDATE fact SET lif_source = ? WHERE extractor = ?", (w, ext))
    if "access_count" not in cols:
        conn.execute("ALTER TABLE fact ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0")
    if "last_accessed_at" not in cols:
        # NULL ⇒ first decay/recency pass treats created_at as last access.
        conn.execute("ALTER TABLE fact ADD COLUMN last_accessed_at TEXT")
    if "seen_sessions" not in cols:
        conn.execute("ALTER TABLE fact ADD COLUMN seen_sessions TEXT NOT NULL DEFAULT '[]'")

    conn.commit()
    _schema_migrated = True


def _decay_one(
    fact: dict[str, Any],
    now: datetime,
    siblings: list[dict[str, Any]],
) -> tuple[dict[str, float], bool]:
    """Recompute a Fact's five-dim LIF (ADR-8v2) and return (dims, deprecate?).

    Supersedes ADR-8's scalar ``original_lif*0.5**(Δt/half_life)`` — the decay
    is folded into the recency dim (``age_h = now - last_accessed_at``, with a
    ``created_at`` fallback = the original ADR-8 decay semantics for first
    access). Calls :func:`scoring.compute_lif` to recompute all five dims
    authoritatively (the consolidate pass owns the coherence recompute per
    ADR-8v2; recall only refreshes freq/recency/spread cheaply).

    ``original_lif`` is no longer the decay base — its semantics shifted to the
    source-dim initial-value snapshot (ADR-8v2). It is passed as
    ``source_override`` only when the fact lacks an extractor (legacy rows), so
    the source dim falls back to the snapshot instead of regex default.

    Idempotent: recency derives from immutable ``created_at`` (until a recall
    refresh moves ``last_accessed_at``), so the same wall clock yields the same
    dims across passes — no compounding. permanent Facts ⇒ recency=1.0.

    Args:
        fact: Decoded Fact dict (carries ``extractor``, ``fact_type``,
            ``created_at``, ``last_accessed_at``, ``access_count``,
            ``seen_sessions``, ``original_lif``).
        now: Pass wall clock (idempotency anchor).
        siblings: Same-subject active Facts (incl. self) for coherence.

    Returns:
        ``(dims, deprecate?)`` where ``dims`` is the :func:`compute_lif` result
        and ``deprecate`` is True iff the composite LIF < threshold.
    """
    # source-dim fallback: ADR-8v2 original_lif⇒source for legacy rows w/o extractor.
    source_override: float | None = None
    if not fact.get("extractor"):
        source_override = float(fact.get("original_lif") or 0.4)

    dims = scoring.compute_lif(
        fact,
        access_count=int(fact.get("access_count") or 0),
        last_accessed_at=fact.get("last_accessed_at"),
        distinct_sessions=len(fact.get("seen_sessions") or []),
        neighbors=siblings,
        now=now,
        source_override=source_override,
    )
    return dims, dims["LIF"] < DEPRECATE_LIF_THRESHOLD


def _object_key(fact: dict[str, Any]) -> str:
    """Identity of the Fact's object side — binary entity→entity uses object_id,
    literal/unary uses value. None coerced to empty string for stable grouping."""
    return fact.get("object_id") or fact.get("value") or ""


def decay() -> dict[str, int]:
    """Run one LIF recompute pass over the active Fact set (ADR-8v2, idempotent).

    For each active Fact: recompute all five LIF dims via
    :func:`scoring.compute_lif` and write back ``LIF`` + the five dim columns.
    Decay (ADR-8 ``original_lif*0.5**(Δt/h)``) is folded into the recency dim
    (``age_h = now - last_accessed_at`` with a ``created_at`` fallback = the
    original ADR-8 first-access semantics); ``created_at`` is immutable, so the
    same wall clock yields the same dims on every call (no compounding) until a
    recall refresh moves ``last_accessed_at``. Facts whose composite LIF drops
    below 0.1 flip ``active → deprecated``.

    Coherence is recomputed authoritatively here (per ADR-8v2 — recall only
    refreshes freq/recency/spread cheaply): the subject's active siblings
    (including self) form the neighbor set.

    Idempotent: re-running with no wall-clock progress produces no LIF change.
    Already-deprecated Facts are excluded so their LIF is frozen at the
    threshold-crossing pass.

    Returns ``{"decayed": <Facts whose LIF/dims changed>, "deprecated": <Facts
    flipped active→deprecated this pass>}``.
    """
    _ensure_schema()
    conn = db.get_conn()
    # ADR-8v2 idempotency anchor: floor wall clock to whole seconds. Sub-second
    # microsecond drift between back-to-back decay() calls would otherwise move
    # age_h (and thus lif_recency) by ~1e-11 — larger than the same-short-circuit
    # tolerance and enough to flip same=False, re-writing every active fact each
    # pass. ms-floor makes "now" identical across sub-second re-runs.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    rows = conn.execute(
        "SELECT * FROM fact WHERE status = 'active'"
    ).fetchall()
    facts = [store._decode_fact(r) for r in rows]

    # Group active facts by subject so _decay_one can read each fact's siblings
    # (incl. self) for the coherence recompute. ponytail: one pass to build the
    # map; O(n) mem. Single-machine MVP ceiling — fine while fact counts are ≤ 1e5.
    by_subject: dict[str, list[dict[str, Any]]] = {}
    for f in facts:
        by_subject.setdefault(f["subject_id"], []).append(f)

    # ADR-8v2 idempotency: same-short-circuit tolerance. 1e-9 absorbs residual
    # clock jitter (the ms-floor on `now` already collapses sub-second drift; a
    # straddled-second boundary can still shift age_h by ±1s ⇒ recency Δ up to
    # ~1e-10 at stable's 90d half-life, far larger than 1e-12). 1e-9 stays well
    # below real threshold crossings (DEPRECATE_LIF_THRESHOLD=0.1) so genuine
    # decay/deprecate events are never masked.
    SAME_TOL = 1e-9
    decayed = 0
    deprecated = 0
    for f in facts:
        siblings = by_subject.get(f["subject_id"], [])
        dims, deprecate = _decay_one(f, now, siblings)
        same = (
            abs(dims["LIF"] - float(f["LIF"])) < SAME_TOL
            and abs(dims["lif_freq"] - float(f.get("lif_freq") or 0)) < SAME_TOL
            and abs(dims["lif_recency"] - float(f.get("lif_recency") or 0)) < SAME_TOL
            and abs(dims["lif_spread"] - float(f.get("lif_spread") or 0)) < SAME_TOL
            and abs(dims["lif_coherence"] - float(f.get("lif_coherence") or 0)) < SAME_TOL
            and abs(dims["lif_source"] - float(f.get("lif_source") or 0)) < SAME_TOL
        )
        if same and not deprecate:
            continue  # no wall-clock progress and no threshold cross — no write
        conn.execute(
            """UPDATE fact SET
                   LIF = ?, lif_freq = ?, lif_recency = ?, lif_spread = ?,
                   lif_coherence = ?, lif_source = ?
               WHERE id = ?""",
            (
                dims["LIF"], dims["lif_freq"], dims["lif_recency"], dims["lif_spread"],
                dims["lif_coherence"], dims["lif_source"], f["id"],
            ),
        )
        decayed += 1
        if deprecate:
            store.update_fact_status(f["id"], "deprecated", valid_to=store._now())
            deprecated += 1
    if decayed:
        conn.commit()
    return {"decayed": decayed, "deprecated": deprecated}


def _group_duplicate_facts(conn: Any) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """Group ACTIVE facts by (subject_id, predicate, object_key); return only
    groups with more than one member (the actual duplicates)."""
    rows = conn.execute(
        "SELECT * FROM fact WHERE status = 'active' ORDER BY created_at ASC"
    ).fetchall()
    facts = [store._decode_fact(r) for r in rows]
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for f in facts:
        key = (f["subject_id"], f["predicate"], _object_key(f))
        groups.setdefault(key, []).append(f)
    return {k: v for k, v in groups.items() if len(v) > 1}


def _merge_group(group: list[dict[str, Any]]) -> int:
    """Collapse one duplicate group to its survivor (first/oldest by created_at).

    ADR-8v2 per-dim merge: the survivor absorbs the max of each LIF dim across
    the group — ``max(lif_freq, lif_spread, lif_source, lif_coherence)`` plus
    ``max(lif_recency)`` — then the composite ``LIF`` is recomputed from the
    absorbed dims via :data:`scoring.LIF_WEIGHTS` (the merge does not re-run
    :func:`compute_lif`; it fuses the stored dim maxima, which is the ADR-literal
    "freq/spread/source/coherence max + recency max + LIF 重算"). The union of
    ``source_refs`` is also absorbed; the rest flip to ``status='superseded'``
    pointing at the survivor. Returns count merged.

    Idempotent: already-superseded dups fall out of the active group on the next
    pass; a re-merge of a single-member group is a no-op.
    """
    survivor = group[0]
    merged = 0
    survivor_id = survivor["id"]

    def _dim(key: str) -> float:
        return max(float(f.get(key) or 0.0) for f in group)

    new_freq = _dim("lif_freq")
    new_recency = _dim("lif_recency")
    new_spread = _dim("lif_spread")
    new_coherence = _dim("lif_coherence")
    new_source = _dim("lif_source")
    w = scoring.LIF_WEIGHTS
    new_lif = max(0.0, min(1.0, (
        w["freq"] * new_freq
        + w["recency"] * new_recency
        + w["spread"] * new_spread
        + w["coherence"] * new_coherence
        + w["source"] * new_source
    )))

    new_refs: list[str] = list(survivor["source_refs"])
    for dup in group[1:]:
        for ref in dup["source_refs"]:
            if ref not in new_refs:
                new_refs.append(ref)
        # ponytail: no transaction — single-writer cli, crash leaves at worst a
        # half-merged group re-runnable on next consolidate (idempotent: already
        # superseded dups fall out of the active group next pass).
        store.update_fact_status(dup["id"], "superseded", supersedes_id=survivor_id, valid_to=store._now())
        merged += 1

    conn = db.get_conn()
    conn.execute(
        """UPDATE fact SET
               LIF = ?, lif_freq = ?, lif_recency = ?, lif_spread = ?,
               lif_coherence = ?, lif_source = ?, source_refs = ?
           WHERE id = ?""",
        (
            new_lif, new_freq, new_recency, new_spread, new_coherence, new_source,
            json.dumps(new_refs, ensure_ascii=False), survivor_id,
        ),
    )
    conn.commit()
    return merged


def consolidate() -> dict[str, int]:
    """Run decay + dedup over the Fact set (Spec §2: decay then dedup).

    Phase 1 decay (ADR-8): LIF *= 0.5**(Δt/half_life); LIF<0.1 flips
    active→deprecated. Phase 2 dedup (ADR-6): exact-duplicate Facts collapse
    to a survivor, the rest flip active→superseded.

    Returns ``{"decayed": ..., "deprecated": ..., "superseded": ...,
    "active": <unique Facts remaining active>}``. Idempotent: a clean run
    with no decay/dups returns zeros.
    """
    conn = db.get_conn()  # ensures schema initialised on first call
    decay_out = decay()
    groups = _group_duplicate_facts(conn)
    superseded = 0
    for _, members in groups.items():
        superseded += _merge_group(members)
    # Active Facts remaining post-merge (dups already flipped to 'superseded').
    active = conn.execute(
        "SELECT COUNT(*) FROM fact WHERE status = 'active'"
    ).fetchone()[0]
    return {
        "decayed": decay_out["decayed"],
        "deprecated": decay_out["deprecated"],
        "superseded": superseded,
        "active": active,
    }
