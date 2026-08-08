"""mem-service scoring — ``score = α·match + β·centrality + γ·LIF`` (ADR-4v2).

Supersedes v1 ``scored = match × lif`` (ADR-4). v2 adds a pagerank centrality
dimension (ADR-2v2): the multiplicative formula was incompatible — centrality=0
would zero out match/LIF. Weighted sum instead.

``match_item`` substring-hit logic is lifted verbatim from
``weighted_recall.py:54-88`` (AO2), but operates on ``Fact.value`` (str) instead
of ``MemoryItem.content`` (the MemoryItem layer does not exist here — ADR-2).

``lif`` reads the ``Fact.LIF`` storage scalar ``∈ [0,1]`` directly (ADR-4):
decoupled from AO2's runtime NeuralField rank-based percentile, which is a
relative field ranking that does not apply to a static trust scalar.

``centrality`` is the pagerank of the fact's most-central connected entity
(ADR-2v2), built on-the-fly in ``recall`` and passed in ``∈ [0,1]``. Default
weights α=0.5/β=0.3/γ=0.2 (ADR-4v2) — tunable as evaluation baseline firms up.

No LLM, unit-testable in isolation.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any


# ── ADR-8v2 LIF five-dim composite ──────────────────────────────────
# All non-LLM, deterministic, unit-testable. decay (ADR-8) is folded into the
# recency dim (last_accessed_at refresh = recall reinforcement).

# Source-dim weight by extractor (canonical home; consolidate/store import this).
SOURCE_WEIGHT: dict[str, float] = {
    "regex": 0.4,
    "llm": 0.7,
    "human": 0.9,
    "vote": 0.85,
}

# Composite weights (sum to 1.0): recency carries the most signal (decay heir),
# then freq (recall saturation), then spread/coherence/source as tie-breakers.
LIF_WEIGHTS: dict[str, float] = {
    "freq": 0.25,
    "recency": 0.30,
    "spread": 0.15,
    "coherence": 0.15,
    "source": 0.15,
}

# half-life (days) per fact_type for the recency dim. permanent ⇒ ∞ ⇒ recency=1.
LIF_HALF_LIFE_DAYS: dict[str, float] = {
    "ephemeral": 7.0,
    "stable": 90.0,
    "permanent": float("inf"),
}

# Hardcoded contradiction predicate pairs (coherence dim). Two facts on the
# same subject with predicates forming a pair count as one conflict. ADR-8v2
# lists "硬编码矛盾对" — these are the minimal semantic opposites; expand by
# evaluation. Order-insensitive, matched both directions.
_CONFLICT_PREDICATE_PAIRS: set[tuple[str, str]] = {
    ("likes", "dislikes"),
    ("uses", "avoids"),
    ("prefers", "rejects"),
    ("supports", "opposes"),
    ("enabled", "disabled"),
}


def query_tokens(query: str) -> list[str]:
    """Case-folded tokenization (mirrors weighted_recall.query_tokens)."""
    return [t for t in (query or "").lower().split() if t]


def match_item(
    content: str | None,
    query_tokens: list[str] | None = None,
    query_lower: str | None = None,
) -> float:
    """``match_item(query, content)`` — substring-hit ratio of query vs content, ``∈ [0, 1]``.

    Lifted verbatim from ``weighted_recall.match_item`` (:54-88) but ``content``
    is a ``str`` (``Fact.value``) instead of ``MemoryItem.content``. The
    MemoryItem layer does not exist in mem-service (ADR-2: Fact reification is
    self-contained), so the field-access ``item.content`` becomes a plain arg.

    Args:
        content: Text to score against (``Fact.value``).
        query_tokens: Pre-tokenized query (optional; derived from query_lower if None).
        query_lower: Lowercased full query (optional; verbatim-phrase hit bonus).

    Returns:
        Match score in ``[0.0, 1.0]``.
    """
    if query_tokens is None:
        query_lower = (query_lower or "").strip().lower()
        query_tokens = [t for t in query_lower.split() if t]

    content_lower = (content or "").lower()

    if not query_tokens:
        return 0.0

    hits = sum(1 for tok in query_tokens if tok and tok in content_lower)
    match = hits / len(query_tokens)
    # verbatim 全句命中加成（最强信号）
    if query_lower and query_lower in content_lower:
        match = min(1.0, match + 0.2)
    return float(max(0.0, min(1.0, match)))


# ADR-4v2 weighted-fusion weights (α·match + β·centrality + γ·LIF).
ALPHA_MATCH = 0.5
BETA_CENTRALITY = 0.3
GAMMA_LIF = 0.2
DELTA_VEC = 0.3  # ADR-13 向量层(vec_sim)默认权重; use_vec 时融合, 默认 off 不改 ADR-4v2 score


# ── mem_score: 记忆质量标量 (LIF + confidence 关联, ADR-15 投影排序用) ──
# synthesis-index 投影排序 + recall _snaptag 用. confidence 落 KG 后静态
# (不随 consolidate 更新) → v1 LIF 主导 + confidence 小权重. TODO 债务:
# consolidation 刷新 confidence 后 weighted/harmonic 才完全自洽.
def _memscore_config() -> tuple[float, float, str]:
    """读 .env: MEM_MEMSCORE_MODE (weighted|harmonic|lif) + W_LIF/W_CONF。cli._load_env 已加载。"""
    mode = (os.environ.get("MEM_MEMSCORE_MODE") or "weighted").strip().lower()
    w_lif = float(os.environ.get("MEM_MEMSCORE_W_LIF") or "0.7")
    w_conf = float(os.environ.get("MEM_MEMSCORE_W_CONF") or "0.3")
    return w_lif, w_conf, mode


def mem_score(fact: dict[str, Any]) -> float:
    """记忆质量标量 ∈ [0,1] = LIF + confidence 关联。
    - weighted (默认 ``MEM_MEMSCORE_MODE=weighted``): w_lif·LIF + w_conf·confidence
    - harmonic: ``2·c·LIF/(c+LIF)`` (两低其一即拉低)
    - lif: 纯 LIF (忽略 confidence)
    纯函数(读 .env mode/权重), 不加列; synthesis 经 fact_id 回 KG 取现值算。"""
    lif = float(fact.get("LIF") or 0.0)
    conf = float(fact.get("confidence") or 0.0)
    _w_lif, _w_conf, mode = _memscore_config()
    if mode == "lif":
        ms = lif
    elif mode == "harmonic":
        ms = 2 * conf * lif / (conf + lif) if (conf + lif) > 0 else 0.0
    else:  # weighted (default)
        ms = _w_lif * lif + _w_conf * conf
    return float(max(0.0, min(1.0, ms)))


def score_fact(
    fact: dict[str, Any],
    query: str,
    *,
    centrality: float = 0.0,
    vec_sim: float = 0.0,
    weights: tuple[float, float, float] | None = None,
    delta: float | None = None,
) -> dict[str, Any]:
    """Score one Fact: ``score = α·match + β·centrality + γ·LIF`` (ADR-4v2).

    ``centrality`` defaults to 0.0 — recall builds the networkx graph and
    passes the fact's entity pagerank (ADR-2v2); callers without a graph
    degrade to α·match + γ·LIF (the v1 signal subset, no zeroing).

    ``weights`` (ADR-4v2 调参): optional ``(α, β, γ)`` override; None ⇒ module
    defaults ``ALPHA_MATCH/BETA_CENTRALITY/GAMMA_LIF``. Grid search (eval_recall)
    passes candidate triples here without monkey-patching module constants.
    ponytail: tuple not dataclass — three floats, no method, no payload worth a
    class. Weights are NOT renormalized (caller controls); the grid freely
    explores un-normalized regions to confirm normalization is irrelevant to
    the blind-spot conclusion.

    Args:
        fact: Decoded Fact dict (must carry ``value`` and ``LIF`` keys).
        query: Recall query text.
        centrality: PageRank of the fact's most-central connected entity,
            normalized to ``[0,1]`` (built on-the-fly by ``recall``).
        weights: Optional ``(α, β, γ)`` fusion weights; None ⇒ ADR-4v2 defaults.

    Returns:
        ``{"fact": fact, "match": float, "centrality": float, "lif": float,
        "score": float}``.
    """
    q_lower = (query or "").strip().lower()
    q_tokens = query_tokens(q_lower)
    m = match_item(fact.get("value"), q_tokens, q_lower)
    # ponytail: lif reads the Fact.LIF storage scalar directly (ADR-4);
    # not AO2 NeuralField rank-based percentile — that is a runtime field
    # ranking, wrong for a static trust scalar.
    lif = float(fact.get("LIF") or 0.0)
    ms = mem_score(fact)  # ADR-15: γ 项改用 mem_score(LIF+confidence 关联), 非裸 LIF
    c = float(centrality or 0.0)
    alpha, beta, gamma = weights if weights is not None else (ALPHA_MATCH, BETA_CENTRALITY, GAMMA_LIF)
    d = DELTA_VEC if delta is None else delta
    vs = float(vec_sim or 0.0)
    score = alpha * m + beta * c + gamma * ms + d * vs
    return {"fact": fact, "match": m, "centrality": c, "lif": lif, "mem_score": ms, "vec_sim": vs, "score": float(score)}


# ── ADR-8v2 LIF five-dim composite ──────────────────────────────────

def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp to an aware datetime; None on failure/empty."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _conflicts(neighbor_preds: list[str]) -> int:
    """Count hardcoded contradiction pairs among a subject's neighbor predicates.

    ``neighbor_preds`` are the predicates of the fact's sibling facts (same
    subject). Each conflicting unordered pair found contributes one conflict;
    duplicates of the same predicate on opposite sides are not double-counted.
    """
    present = {p for p in (neighbor_preds or []) if p}
    n = 0
    for a, b in _CONFLICT_PREDICATE_PAIRS:
        if a in present and b in present:
            n += 1
    return n


def compute_lif(
    fact: dict[str, Any],
    access_count: int,
    last_accessed_at: str | None,
    distinct_sessions: int,
    neighbors: list[dict[str, Any]] | list[str] | None,
    *,
    now: datetime | None = None,
    source_override: float | None = None,
    lif_weights: dict[str, float] | None = None,
) -> dict[str, float]:
    """Compose LIF from five non-LLM dimensions (ADR-8v2).

    Pure function — no DB, no side effects, deterministic given inputs.

    Dimensions:
        freq: ``1 - exp(-access_count/5)`` — recall saturation.
        recency: ``exp(-ln2 · age_h / half_life_h)``, ``age_h = now -
            last_accessed_at`` (falls back to ``fact["created_at"]`` when
            ``last_accessed_at`` is None — first access treats store time as
            last access). half_life from ``fact_type`` (ephemeral=7d /
            stable=90d / permanent=∞⇒1.0). Absent timestamps ⇒ recency=0.5
            (mid-neutral, never zero-stamp a fact to death on a parse miss).
            Decay (ADR-8 ``original_lif*0.5**(Δt/h)``) is folded into this dim:
            ``last_accessed_at`` refresh on recall is the reinforcement path.
        spread: ``min(1, distinct_sessions/5)`` — cross-session breadth.
        coherence: ``1 - conflicts/max(1, len(neighbors))`` over hardcoded
            contradiction pairs among neighbor predicates. Empty neighbors ⇒ 1.0.
        source: ``SOURCE_WEIGHT[extractor]`` (regex=0.4/llm=0.7/human=0.9/
            vote=0.85), unknown extractor ⇒ 0.4. ``source_override`` (when
            given) replaces this — used by consolidate's decay pass to honour
            ADR-8v2's ``original_lif``⇒source fallback for legacy rows whose
            extractor is absent.

    Composite: ``LIF = w_f·freq + w_r·recency + w_s·spread + w_c·coherence +
    w_o·source`` (0.25/0.30/0.15/0.15/0.15).

    Args:
        fact: Fact dict (carries ``extractor``, ``fact_type``, ``created_at``).
        access_count: Cumulative recall hits.
        last_accessed_at: ISO ts of last recall (None ⇒ created_at fallback).
        distinct_sessions: Count of distinct sessions that recalled this fact.
        neighbors: Sibling facts (same subject) for coherence. Accepts either
            Fact dicts (predicate read from ``["predicate"]``) or bare predicate
            strings. None / empty ⇒ coherence=1.0 (no contradictions possible).
        now: Override for deterministic tests; defaults to utcnow.
        source_override: When given, force the source dim to this value
            (clamped) instead of deriving from ``extractor``. ADR-8v2:
            ``original_lif`` is the source-dim fallback — consolidate passes
            it here for facts with no extractor.
        lif_weights: Optional override for the five composite dim weights
            (keys: freq/recency/spread/coherence/source). None ⇒ LIF_WEIGHTS.
            ADR-4v2 调参 — eval_recall grid 可传候选 dict 不 monkey-patch
            模块常量。Missing keys fall back to LIF_WEIGHTS (partial override).

    Returns:
        ``{"LIF": float, "lif_freq": float, "lif_recency": float,
        "lif_spread": float, "lif_coherence": float, "lif_source": float}``,
        all clamped to ``[0,1]``.
    """
    # ADR-8v2 idempotency: default `now` floored to whole seconds so back-to-
    # back decay() calls sample the same wall clock (no microsecond drift ⇒
    # identical recency ⇒ same-short-circuit holds). Callers pass explicit `now`
    # for tests; decay() also floors its own sample.
    now = now or datetime.now(timezone.utc).replace(microsecond=0)

    # freq — recall saturation.
    freq = 1.0 - math.exp(-float(access_count or 0) / 5.0)

    # recency — decay folded in (ADR-8v2). last_accessed_at ⇒ created_at fallback.
    half_life = LIF_HALF_LIFE_DAYS.get(fact.get("fact_type") or "stable", 90.0)
    if half_life == float("inf"):
        recency = 1.0
    else:
        last_dt = _parse_iso(last_accessed_at) or _parse_iso(fact.get("created_at"))
        if last_dt is None:
            recency = 0.5  # unparseable timestamps: mid-neutral, not death.
        else:
            age_h = max(0.0, (now - last_dt).total_seconds() / 3600.0)
            half_life_h = half_life * 24.0
            recency = math.exp(-math.log(2) * age_h / half_life_h)

    # spread — cross-session breadth.
    spread = min(1.0, float(distinct_sessions or 0) / 5.0)

    # coherence — subject-neighbor agreement over hardcoded contradiction pairs.
    neighbor_list = neighbors or []
    neighbor_preds = [
        n["predicate"] if isinstance(n, dict) else str(n)
        for n in neighbor_list
    ]
    n_neigh = len(neighbor_list)
    if n_neigh == 0:
        coherence = 1.0
    else:
        conflicts = _conflicts(neighbor_preds)
        coherence = max(0.0, 1.0 - conflicts / max(1, n_neigh))

    # source — extractor trust. source_override (ADR-8v2 original_lif⇒source
    # fallback for legacy rows) takes precedence when given.
    if source_override is not None:
        source = float(source_override)
    else:
        source = SOURCE_WEIGHT.get(fact.get("extractor") or "regex", 0.4)

    # ponytail: dict merge for partial override — copy + update avoids
    # "mutate module global" footgun and lets grid pass sparse overrides.
    w = dict(LIF_WEIGHTS)
    if lif_weights:
        w.update({k: float(v) for k, v in lif_weights.items()})
    lif = (
        w["freq"] * freq
        + w["recency"] * recency
        + w["spread"] * spread
        + w["coherence"] * coherence
        + w["source"] * source
    )
    return {
        "LIF": float(max(0.0, min(1.0, lif))),
        "lif_freq": float(max(0.0, min(1.0, freq))),
        "lif_recency": float(max(0.0, min(1.0, recency))),
        "lif_spread": float(max(0.0, min(1.0, spread))),
        "lif_coherence": float(max(0.0, min(1.0, coherence))),
        "lif_source": float(max(0.0, min(1.0, source))),
    }


def refresh_lif_on_recall(
    fact_id: str,
    *,
    session_id: str | None = None,
    conn: Any = None,
) -> dict[str, float] | None:
    """Recall-reinforcement: bump access stats, recompute LIF, write back (ADR-8v2).

    On each recall of ``fact_id``: ``access_count += 1``,
    ``last_accessed_at = now``, ``seen_sessions`` absorbs ``session_id`` (if
    given and not already present), then LIF is recomputed via
    :func:`compute_lif` (freq/recency/spread refresh; coherence reads current
    subject siblings) and the eight LIF columns + ``LIF`` are written back.

    LIF recomputation is deterministic given the stored state — calling this
    twice advances freq/recency/spread each time (the reinforcement signal),
    but two calls from identical recall state (same access_count/sessions) yield
    the same LIF. No compounding drift across passes.

    Args:
        fact_id: Fact to reinforce.
        session_id: Session doing the recall (drives lif_spread). None ⇒ no
            session change, only access_count/last_accessed_at advance.
        conn: Optional open connection (single-writer cli reuse); default opens
            via ``db.get_conn()``.

    Returns:
        The :func:`compute_lif` result dict, or None if ``fact_id`` not found.
    """
    import db
    import store

    conn = conn or db.get_conn()
    row = conn.execute("SELECT * FROM fact WHERE id = ?", (fact_id,)).fetchone()
    if row is None:
        return None
    fact = store._decode_fact(row)

    # ADR-8v2 ms-floor: match compute_lif/decay's floor so last_accessed_at
    # and the now passed to compute_lif carry no microseconds (precision
    # symmetry with the decay idempotency anchor — see consolidate.decay).
    now = datetime.now(timezone.utc).replace(microsecond=0)
    now_iso = now.isoformat()
    access_count = int(fact.get("access_count") or 0) + 1

    sessions: list[str] = list(fact.get("seen_sessions") or [])
    if session_id and session_id not in sessions:
        sessions.append(session_id)
    distinct_sessions = len(sessions)

    # coherence: read current subject siblings, INCLUDING self — a fact's own
    # predicate pairs against its siblings (uses+avoids are conflicting only
    # when both the fact and a sibling hold one of the pair). Excluding self
    # drops half of every pair, so contradictions never fire. ponytail: linear
    # scan of subject's active facts — consolidate recomputes authoritatively,
    # recall only needs a cheap approximation for the spread/freq/recency refresh.
    sib_rows = conn.execute(
        "SELECT predicate FROM fact WHERE subject_id = ? AND id != ? AND status = 'active'",
        (fact["subject_id"], fact_id),
    ).fetchall()
    own_pred = fact.get("predicate")
    neighbors = (
        ([{"predicate": own_pred}] if own_pred else [])
        + [{"predicate": r["predicate"]} for r in sib_rows]
    )

    dims = compute_lif(
        fact,
        access_count=access_count,
        last_accessed_at=now_iso,
        distinct_sessions=distinct_sessions,
        neighbors=neighbors,
        now=now,
    )

    conn.execute(
        """UPDATE fact SET
               LIF = ?, lif_freq = ?, lif_recency = ?, lif_spread = ?,
               lif_coherence = ?, lif_source = ?,
               access_count = ?, last_accessed_at = ?, seen_sessions = ?
           WHERE id = ?""",
        (
            dims["LIF"], dims["lif_freq"], dims["lif_recency"], dims["lif_spread"],
            dims["lif_coherence"], dims["lif_source"],
            access_count, now_iso, json.dumps(sessions, ensure_ascii=False),
            fact_id,
        ),
    )
    conn.commit()
    return dims


__all__ = [
    "query_tokens", "match_item", "score_fact", "mem_score",
    "ALPHA_MATCH", "BETA_CENTRALITY", "GAMMA_LIF",
    "compute_lif", "refresh_lif_on_recall",
    "SOURCE_WEIGHT", "LIF_WEIGHTS", "LIF_HALF_LIFE_DAYS",
]
