"""mem-service recall — KG navigation + on-the-fly pagerank centrality (ADR-2v2, ADR-4v2).

Pipeline: split query → ``search_entities`` (entity.name LIKE per token)
→ entity is the subject/object of its Facts → gather candidate Facts → build a
networkx graph from active facts (entity nodes + fact edges) → pagerank →
score ``α·match + β·centrality + γ·LIF`` (ADR-4v2) → sort desc → return Fact list.

Graph construction is **on-the-fly** (ADR-2v2): rebuilt every recall from the
SQLite active-fact set, never persisted, no ingest-time maintenance. O(V+E) per
recall is acceptable for single-machine MVP.

Returns Facts, never MemoryItems (the MemoryItem layer does not exist — ADR-2).
``--verbose`` (``verbose=True``) exposes per-Fact hit detail (entity/match/
centrality/lif/score) as a debug surface in lieu of a dedicated ``query`` cli.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db
import embedding
import networkx as nx
import projection
import scoring
import store

# ADR-13 向量层(use_vec): 候选扩展 + vec_sim 阈值/top-N。
VEC_MIN = 0.30   # cosine ≥ 此的 active fact 入向量候选(避免全 noise 污染 top-k)
VEC_TOP_N = 20   # 向量候选上限(扩展 entity/value 候选集)


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity; 0.0 on empty/zero-norm. ponytail: 纯 Python, 无 numpy 依赖。"""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _build_centralities() -> dict[str, float]:
    """On-the-fly pagerank centrality per entity, normalized to ``[0,1]`` (ADR-2v2).

    Builds a networkx graph from all active facts: each fact is an edge between
    its subject entity and (if present) object entity. Runs ``nx.pagerank``,
    then min-max normalizes (max → 1.0) so the most central entity carries the
    full β weight. Rebuilt every recall — no persistence.

    Returns ``{entity_id: centrality ∈ [0,1]}``. Empty when no active facts.
    """
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT subject_id, object_id FROM fact WHERE status='active'"
    ).fetchall()
    g = nx.Graph()
    seen: set[str] = set()
    for r in rows:
        s = r["subject_id"]
        o = r["object_id"]
        seen.add(s)
        if o:
            seen.add(o)
            g.add_edge(s, o)
        else:
            g.add_node(s)
    if not seen:
        return {}
    pr = nx.pagerank(g) if g.number_of_edges() else {n: 0.0 for n in g.nodes}
    # Isolated-only graph → all zero. Edges present → min-max normalize max→1.0.
    mx = max(pr.values()) if pr else 0.0
    if mx <= 0.0:
        return {eid: 0.0 for eid in seen}
    return {eid: pr.get(eid, 0.0) / mx for eid in seen}


def _fact_centrality(fact: dict[str, Any], centrality: dict[str, float]) -> float:
    """Centrality of a fact = pagerank of its most-central connected entity."""
    cands = [centrality.get(fact.get("subject_id") or "", 0.0)]
    oid = fact.get("object_id")
    if oid:
        cands.append(centrality.get(oid, 0.0))
    return max(cands) if cands else 0.0


def search_entities(tokens: list[str]) -> list[dict[str, Any]]:
    """Return entities whose ``name`` LIKE-matches any query token (case-insensitive).

    KG navigation entry: an entity that names a query concept is the anchor
    whose Facts (as subject or object) carry the answer.
    """
    if not tokens:
        return []
    conn = db.get_conn()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for tok in tokens:
        pat = f"%{tok}%"
        rows = conn.execute(
            "SELECT * FROM entity WHERE lower(name) LIKE ?", (pat.lower(),)
        ).fetchall()
        for r in rows:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            out.append({
                "id": r["id"], "name": r["name"], "entity_type": r["entity_type"],
            })
    return out


def _facts_for_entities(entity_ids: list[str]) -> list[dict[str, Any]]:
    """Facts where any entity is subject_id OR object_id (status='active')."""
    if not entity_ids:
        return []
    conn = db.get_conn()
    placeholders = ",".join("?" * len(entity_ids))
    rows = conn.execute(
        f"SELECT * FROM fact WHERE status='active' AND "
        f"(subject_id IN ({placeholders}) OR object_id IN ({placeholders}))",
        (*entity_ids, *entity_ids),
    ).fetchall()
    facts: list[dict[str, Any]] = []
    for r in rows:
        facts.append(store._decode_fact(r))
    return facts


def recall(
    query: str,
    *,
    verbose: bool = False,
    top_k: int | None = None,
    session_id: str | None = None,
    boost: bool = True,
    weights: tuple[float, float, float] | None = None,
    use_vec: bool = False,
    delta: float | None = None,
    cwd: str | None = None,
    with_tag: bool = False,
    mem_dir: str | Path | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Recall Facts relevant to ``query``, ranked by ``α·match + β·centrality + γ·LIF``.

    KG navigation: ``entity.name LIKE`` per query token anchors entities; their
    Facts (subject or object) are the candidate set. Score = ``match_item`` on
    ``Fact.value`` + on-the-fly pagerank centrality (ADR-2v2) + ``Fact.LIF``
    scalar, fused by ADR-4v2 weighted sum.

    Recall reinforcement (ADR-8v2): after ranking + top_k truncation, each
    returned (hit) Fact's access stats are refreshed via
    :func:`scoring.refresh_lif_on_recall` — ``access_count += 1``,
    ``last_accessed_at = now``, ``seen_sessions`` absorbs ``session_id`` — and
    LIF is recomputed (freq/recency/spread rise; the reinforcement feedback
    loop). Set ``boost=False`` to skip (read-only recall). ``session_id=None``
    still bumps access_count/last_accessed_at (spread stays put).

    Args:
        query: Recall query text.
        verbose: When True, return ``{"fact":..., "match":..., "centrality":...,
            "lif":..., "score":..., "entities":[...]}`` dicts (debug detail for
            ``recall --verbose``); else bare Fact dicts.
        top_k: Truncate to top-k by score; None = no truncation.
        session_id: Session doing the recall (drives lif_spread on boost).
        boost: Refresh LIF on hit facts (ADR-8v2 reinforcement); default True.
        weights: Optional ``(α, β, γ)`` override forwarded to ``score_fact``
            (ADR-4v2 调参); None ⇒ module defaults. eval_recall grid passes
            candidate triples here.

    Returns:
        Sorted list of Facts (bare) or score-detail dicts (verbose).
        Empty list when no entity matches or no Facts score.
    """
    tokens = scoring.query_tokens(query)
    entities = search_entities(tokens)
    # ponytail: also surface candidate facts whose value literally contains a
    # query token, even when no entity.name matched — covers literal facts whose
    # subject name does not echo the query (e.g. "用户" subject, "rust" in value).
    # Ceiling: linear scan of all active facts; fine for single-machine MVP.
    conn = db.get_conn()
    # ADR-14 cwd 隔离(b 方案): cwd 给定时只扫该 cwd 的 fact(+ NULL 老数据兼容)。
    if cwd:
        value_rows = conn.execute(
            "SELECT * FROM fact WHERE status='active' AND (source_cwd = ? OR source_cwd IS NULL)",
            (cwd,),
        ).fetchall()
    else:
        value_rows = conn.execute("SELECT * FROM fact WHERE status='active'").fetchall()
    seen_ids: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for r in _facts_for_entities([e["id"] for e in entities]):
        if r["id"] not in seen_ids:
            seen_ids.add(r["id"])
            candidates.append(r)
    for r in value_rows:
        rid = r["id"]
        if rid in seen_ids:
            continue
        val = (r["value"] or "").lower()
        if any(tok and tok in val for tok in tokens):
            seen_ids.add(rid)
            candidates.append(store._decode_fact(r))

    # ADR-13 向量候选扩展(use_vec): query embed → cosine vs active fact.value →
    # top-N 加入候选集。解 synonym/rewrite 字面盲区(铁锈↔rust cosine 信号 > 字面 0)。
    # embedding passive([]); use_vec 且 qv 空时跳过(回退纯字面/centrality/LIF)。
    qv = embedding.embed(query) if use_vec else []
    if qv:
        vec_cands: list[tuple[dict[str, Any], float]] = []
        for r in value_rows:
            rid = r["id"]
            val = r["value"]
            if not val or rid in seen_ids:
                continue
            fv = embedding.embed(val)
            if not fv:
                continue
            sim = _cosine(qv, fv)
            if sim >= VEC_MIN:
                vec_cands.append((store._decode_fact(r), sim))
        vec_cands.sort(key=lambda x: -x[1])
        for f, _sim in vec_cands[:VEC_TOP_N]:
            if f["id"] not in seen_ids:
                seen_ids.add(f["id"])
                candidates.append(f)

    # ADR-14 cwd 过滤 entity-based candidates(entity 可跨 cwd, Python 过滤; NULL 兼容老数据)
    if cwd:
        candidates = [f for f in candidates
                      if not f.get("source_cwd") or f["source_cwd"] == cwd]
        seen_ids = {f["id"] for f in candidates}

    # ADR-2v2: on-the-fly pagerank centrality over the full active-fact graph
    # (one build per recall, no persistence). Each fact's centrality = the
    # pagerank of its most-central connected entity.
    centralities = _build_centralities()
    scored = []
    for f in candidates:
        vs = 0.0
        if qv and f.get("value"):
            fv = embedding.embed(f["value"])  # cache hit(向量候选扩展已 embed)
            vs = _cosine(qv, fv) if fv else 0.0
        scored.append(scoring.score_fact(
            f, query,
            centrality=_fact_centrality(f, centralities),
            vec_sim=vs, weights=weights, delta=delta,
        ))
    # drop low-score (噪音) 除非 verbose wants them; ADR-4v2 α=0.5 对齐: match≥0.6 达标,
    # 总 score 门槛 0.3(保留中高质量 hit, 降噪)。vec_sim>0 的候选 score>0 不 drop — 即使字面 m=0(盲区解)。
    scored = [s for s in scored if s["score"] >= 0.3]
    scored.sort(key=lambda s: s["score"], reverse=True)
    if top_k is not None:
        scored = scored[: max(0, top_k)]

    # ADR-8v2 recall reinforcement: refresh access stats + recompute LIF on
    # each hit fact. freq/recency/spread rise on recall (the feedback loop);
    # the returned facts carry the refreshed LIF. boost=False ⇒ pure read.
    # ponytail: linear refresh over the (already top_k-bounded) hit set; O(k)
    # UPDATEs, k typically ≤ top_k. Idempotent within a wall clock — refresh
    # recomputes from stored state, no compounding drift (cf. scoring contract).
    if boost and scored:
        conn = db.get_conn()
        for s in scored:
            refreshed = scoring.refresh_lif_on_recall(
                s["fact"]["id"], session_id=session_id, conn=conn,
            )
            if refreshed is not None:
                # Reflect the post-reinforcement stored state on the returned
                # FACT. refresh_lif_on_recall is the authority (it writes
                # access_count/last_accessed_at/seen_sessions + recomputes LIF);
                # re-reading the row avoids hand-replaying those fields off the
                # stale pre-refresh dict (off-by-N if a caller ever hands us a
                # dict already aligned with the store). The verbose dict's own
                # ``lif``/``score`` fields stay at score-time values — they pin
                # the ADR-4v2 identity score; the reinforced scalar is read off
                # fact["LIF"].
                authoritative = store.get_fact(s["fact"]["id"])
                if authoritative is not None:
                    s["fact"].update(authoritative)

    # ADR-15 Ch2: 命中 fact → 建/刷 mem-<id>.md (snaptag 物化) + 算 tag 嵌 _snaptag。
    # 绝不碰 MEMORY.md(撞 autoMemory cache)。mem_dir 优先显式, 否则 cwd 推导;
    # 都无则只算 tag 不建文件(测试/无 cwd 场景)。
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    ent_names: dict[str, str] = {}
    if scored:
        eids = {s["fact"].get("subject_id") for s in scored}
        eids |= {s["fact"].get("object_id") for s in scored}
        eids.discard(None)
        if eids:
            erows = conn.execute(
                f"SELECT id, name FROM entity WHERE id IN ({','.join('?' * len(eids))})",
                tuple(eids),
            ).fetchall()
            ent_names = {r["id"]: r["name"] for r in erows}
    mem_dir_obj = Path(mem_dir) if mem_dir else (projection.cc_memory_dir(cwd) if cwd else None)
    for s in scored:
        f = s["fact"]
        fid = f["id"]
        subj = ent_names.get(f.get("subject_id"), "?")
        # ADR-A/C: 投影标题 = topic(回退三元组); 与 project_fact_md 内部一致。
        topic = projection._fact_topic(f, subj)
        ms = scoring.mem_score(f)
        mem_path = None
        if mem_dir_obj is not None:
            projection.project_fact_md(f, subj, mem_dir_obj, recalled_at=now_iso)
            # ADR-B: 相对路径与 MEMORY.md 同目录 → 仅文件名(无 memory/ 前缀)。
            mem_path = projection._mem_filename(fid, topic)
        tag = {
            "fact_id": fid,
            "mem_path": mem_path,
            "kg_uri": f"kg://fact/{fid}",
            "display": topic,
            "topic": topic,
            "mem_score": ms,
            "recalled_at": now_iso,
            "session_id": session_id,
        }
        f["_snaptag"] = tag  # 默认 list[dict] shape 嵌此字段(向后兼容, 调用方可忽略)
        s["tag"] = tag

    if verbose:
        ent_ids = {e["id"] for e in entities}
        for s in scored:
            f = s["fact"]
            s["entities"] = [
                eid for eid in (f.get("subject_id"), f.get("object_id")) if eid in ent_ids
            ]
        return scored
    if with_tag:
        return {
            "query": query,
            "session_id": session_id,
            "results": [{"fact": s["fact"], "score": s["score"], "tag": s["tag"]} for s in scored],
        }
    return [s["fact"] for s in scored]


__all__ = ["search_entities", "recall"]
