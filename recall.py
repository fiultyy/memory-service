"""mem-service recall — KG navigation + on-the-fly pagerank centrality (ADR-2v2, ADR-4v2).

Pipeline: split query → ``search_entities`` (entity.name LIKE per token)
→ entity is the subject/object of its Facts → gather candidate Facts → build a
networkx graph from active facts (entity nodes + fact edges) → pagerank →
score ``α·match + β·centrality + γ·LIF`` (ADR-4v2) → sort desc → return Fact list.

Optional BFS expansion (``use_bfs=True``): seed entities found by name match
are expanded via graph BFS up to ``bfs_hops`` hops; expanded facts bypass the
``score >= 0.3`` hard filter and contribute ``bfs_proximity`` to scoring.
The entity graph is built once and shared between centrality and BFS.

Graph construction is **on-the-fly** (ADR-2v2): rebuilt every recall from the
SQLite active-fact set, never persisted, no ingest-time maintenance. O(V+E) per
recall is acceptable for single-machine MVP.

Returns Facts, never MemoryItems (the MemoryItem layer does not exist — ADR-2).
``--verbose`` (``verbose=True``) exposes per-Fact hit detail (entity/match/
centrality/lif/score) as a debug surface in lieu of a dedicated ``query`` cli.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db
import embedding
import networkx as nx
import projection
import scoring
import signals
import store

# ADR-13 向量层(use_vec): 候选扩展 + vec_sim 阈值/top-N。
# M13 (G5 已裁决): BFS 扩展入场门槛 — seed/hop 邻居经扩展通道入场的 fact 需
# lif_source ≥ _BFS_SOURCE_GATE (0.7): regex 0.4 档占位噪声拒、llm 0.7/vote
# 0.85/human 0.9 过。BFS 是增益通道非主检索 — 字面 seed/向量/中心性路径不受
# 门槛影响 (低 source fact 仍可被直接查询召回)。hop>0 绕 0.3 过滤的 §7 语义
# 保留, 但只对过了本门槛入场的扩展 fact 生效 (低分邻居的边不因 bypass 入图
# — 门槛在 append 点, 未入场者永不进 bfs_expanded_ids)。[设] 可调。
_BFS_SOURCE_GATE = 0.7
VEC_MIN = 0.30   # cosine ≥ 此的 active fact 入向量候选(避免全 noise 污染 top-k)
VEC_TOP_N = 20   # 向量候选上限(扩展 entity/value 候选集)
# ADR-4 bfs hint: direct-match 薄(候选 < 阈值)且 use_bfs=False 时 suggest_bfs。
SUGGEST_BFS_THRESHOLD = 3


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity; 0.0 on empty/zero-norm. ponytail: 纯 Python, 无 numpy 依赖。"""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0

def _temporal_clause(as_of: str | None = None) -> tuple[str, list]:
    """Fact validity WHERE fragment + bind params (status + temporal).

    Default (as_of=None): ``status='active' AND valid_to IS NULL`` — only
    currently-valid facts (zero-regression with pre-D4 status='active' filter).
    as_of given: point-in-time bi-temporal query — ``(valid_from IS NULL OR
    valid_from <= ?) AND (valid_to IS NULL OR valid_to > ?)``.  The status
    filter is dropped: a superseded fact was valid at a historical moment.
    NULL valid_from = -inf (always <= as_of).  Returns ``(sql, params)``.

    隐式假设(ADR-3 ③): SQLite TEXT 字典序 = 时间序。成立前提是所有 valid_from/
    valid_to/as_of 均为同一归一格式 —— store._now() 统一 +00:00 秒级 ISO-8601
    (ms-floor), cli 把 --as-of 归一为 UTC +00:00 再下传。任一处混入非 UTC 后缀
    (如 +08:00) 或不同精度即字典序错序, 故归一只在 cli 输入端做(store 不再二次
    归一, 避免覆盖显式 valid_from 参数)。
    """
    if as_of is None:
        return ("status='active' AND valid_to IS NULL", [])
    return (
        "(valid_from IS NULL OR valid_from <= ?) AND (valid_to IS NULL OR valid_to > ?)",
        [as_of, as_of],
    )


def _build_entity_graph(
    as_of: str | None = None, source_cwd: str | None = None,
) -> tuple[nx.Graph, dict[str, float]]:
    """Build entity graph from active facts + pagerank centrality (ADR-2v2).

    Returns ``(graph, centrality_dict)``.  The graph is an undirected
    ``nx.Graph`` where each active fact links ``subject_id ↔ object_id``.
    ``centrality_dict`` is pagerank normalised to ``[0,1]``.  Single-source
    build shared by centrality scoring and BFS expansion.

    ``source_cwd`` (ADR-4 scoped opt-in, default None = global graph per ADR-14
    单体 KG 跨 cwd 共享): when set, only facts whose ``source_cwd`` matches or
    is NULL contribute edges — graph more precise but smaller, for cross-cwd
    noise reduction. NULL 兼容老数据(无 source_cwd 的 fact 视为全局)。
    """
    conn = db.get_conn()
    tc, tp = _temporal_clause(as_of)
    if source_cwd is None:
        rows = conn.execute(
            f"SELECT subject_id, object_id FROM fact WHERE {tc}",
            tp,
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT subject_id, object_id FROM fact WHERE {tc} "
            f"AND (source_cwd = ? OR source_cwd IS NULL)",
            (*tp, source_cwd),
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
        return (g, {})
    pr = nx.pagerank(g) if g.number_of_edges() else {n: 0.0 for n in g.nodes}
    # Isolated-only graph → all zero. Edges present → min-max normalize max→1.0.
    mx = max(pr.values()) if pr else 0.0
    if mx <= 0.0:
        return (g, {eid: 0.0 for eid in seen})
    return (g, {eid: pr.get(eid, 0.0) / mx for eid in seen})


def _build_centralities(as_of: str | None = None) -> dict[str, float]:
    """Thin wrapper: returns centrality dict only (backward compatible)."""
    return _build_entity_graph(as_of=as_of)[1]


def _fact_centrality(fact: dict[str, Any], centrality: dict[str, float]) -> float:
    """Centrality of a fact = pagerank of its most-central connected entity."""
    cands = [centrality.get(fact.get("subject_id") or "", 0.0)]
    oid = fact.get("object_id")
    if oid:
        cands.append(centrality.get(oid, 0.0))
    return max(cands) if cands else 0.0

def _hop_decay(hop: int) -> float:
    """BFS hop → proximity weight.  hop 0→1.0, 1→0.5, 2→0.25, <0→0.0."""
    if hop < 0:
        return 0.0
    return 1.0 if hop == 0 else 0.5 ** hop


def bfs_neighbors(
    seed_entity_ids: list[str],
    graph: nx.Graph,
    hops: int = 2,
    max_nodes: int = 50,
) -> dict[str, int]:
    """BFS from *seed_entity_ids* over *graph*, return ``{entity_id: min_hop}``.

    Seed entities are hop 0.  Uses ``nx.single_source_shortest_path_length``
    per seed and merges keeping the minimum hop per entity.  Caps total
    returned entities at *max_nodes* (lowest hop first).
    """
    if not seed_entity_ids or graph.number_of_nodes() == 0:
        return {}
    merged: dict[str, int] = {}
    for seed in seed_entity_ids:
        if seed not in graph:
            continue
        lengths = nx.single_source_shortest_path_length(graph, seed, cutoff=hops)
        for eid, dist in lengths.items():
            prev = merged.get(eid)
            if prev is None or dist < prev:
                merged[eid] = dist
    if len(merged) > max_nodes:
        # keep lowest hop first, then cap
        by_hop = sorted(merged.items(), key=lambda x: x[1])
        merged = dict(by_hop[:max_nodes])
    return merged


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


def _facts_for_entities(entity_ids: list[str], as_of: str | None = None) -> list[dict[str, Any]]:
    """Facts where any entity is subject_id OR object_id (status='active')."""
    if not entity_ids:
        return []
    conn = db.get_conn()
    tc, tp = _temporal_clause(as_of)
    placeholders = ",".join("?" * len(entity_ids))
    rows = conn.execute(
        f"SELECT * FROM fact WHERE {tc} AND "
        f"(subject_id IN ({placeholders}) OR object_id IN ({placeholders}))",
        (*tp, *entity_ids, *entity_ids),
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
    use_bfs: bool = False,
    bfs_hops: int = 2,
    as_of: str | None = None,
    use_bfs_scoped: bool = False,
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
    tc, tp = _temporal_clause(as_of)
    if cwd:
        value_rows = conn.execute(
            f"SELECT * FROM fact WHERE {tc} AND (source_cwd = ? OR source_cwd IS NULL)",
            (*tp, cwd),
        ).fetchall()
    else:
        value_rows = conn.execute(
            f"SELECT * FROM fact WHERE {tc}",
            tp,
        ).fetchall()
    seen_ids: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for r in _facts_for_entities([e["id"] for e in entities], as_of=as_of):
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
    # BFS 扩展(use_bfs): 同一次图构建复用于 centrality + BFS, 不重复 build。
    entity_graph, centralities = _build_entity_graph(
        as_of=as_of, source_cwd=(cwd if use_bfs_scoped else None),
    )
    # BFS 图遍历扩展(use_bfs): 从 seed entity BFS 扩展邻居, 补充字面/向量未命中的 fact。
    bfs_fact_min_hop: dict[str, int] = {}
    bfs_expanded_ids: set[str] = set()
    if use_bfs and entities:
        seed_ids = [e["id"] for e in entities]
        bfs_result = bfs_neighbors(seed_ids, entity_graph, hops=bfs_hops)
        # 邻居实体(非 seed) → 取回 fact 扩展候选集
        neighbor_eids = [eid for eid in bfs_result if eid not in seed_ids]
        if neighbor_eids:
            for f in _facts_for_entities(neighbor_eids, as_of=as_of):
                if float(f.get("lif_source") or 0.0) < _BFS_SOURCE_GATE:
                    continue  # M13 G5: BFS 扩展入场门槛 lif_source≥0.7
                fid = f["id"]
                if fid not in seen_ids:
                    seen_ids.add(fid)
                    candidates.append(f)
        # 记录每个 fact 的 min_hop(基于 subject_id/object_id 在 bfs_result 中的最小值)
        for f in candidates:
            fid = f["id"]
            sub = f.get("subject_id")
            obj = f.get("object_id")
            hops_for_fact: list[int] = []
            if sub and sub in bfs_result:
                hops_for_fact.append(bfs_result[sub])
            if obj and obj in bfs_result:
                hops_for_fact.append(bfs_result[obj])
            if hops_for_fact:
                min_h = min(hops_for_fact)
                # 如果该 fact 是 BFS 扩展来的(hop>0), 追踪
                if min_h > 0:
                    bfs_expanded_ids.add(fid)
                prev = bfs_fact_min_hop.get(fid)
                if prev is None or min_h < prev:
                    bfs_fact_min_hop[fid] = min_h
    scored = []
    for f in candidates:
        vs = 0.0
        if qv and f.get("value"):
            fv = embedding.embed(f["value"])  # cache hit(向量候选扩展已 embed)
            vs = _cosine(qv, fv) if fv else 0.0
        bfs_prox = _hop_decay(bfs_fact_min_hop.get(f["id"], -1)) if use_bfs else 0.0
        scored.append(scoring.score_fact(
            f, query,
            centrality=_fact_centrality(f, centralities),
            vec_sim=vs, weights=weights, delta=delta,
            bfs_proximity=bfs_prox,
        ))
    # drop low-score (噪音); BFS 扩展 fact(hop>0) 绕过 0.3 门槛但仍参与排序+top_k。
    scored = [s for s in scored if s["score"] >= 0.3 or s["fact"]["id"] in bfs_expanded_ids]
    scored.sort(key=lambda s: s["score"], reverse=True)
    if top_k is not None:
        scored = scored[: max(0, top_k)]

    # ADR-8v2 recall reinforcement: refresh access stats + recompute LIF on
    # each hit fact. freq/recency/spread rise on recall (the feedback loop);
    # the returned facts carry the refreshed LIF. boost=False ⇒ pure read.
    # ponytail: linear refresh over the (already top_k-bounded) hit set; O(k)
    # UPDATEs, k typically ≤ top_k. Idempotent within a wall clock — refresh
    # recomputes from stored state, no compounding drift (cf. scoring contract).
    #
    # M10 (DR-1 D3 / DR-7 G7 已裁决): env 灰度 MEM_DELAYED_REINFORCE=1 时强化
    # 改道 — 即时写回关闭 (等效 boost=False 纯读, recall 读路径无写争用),
    # 每命中 fact 追加一条 recall_hits 信号 (M5 流, LIF 重算移入 dreaming 批量
    # 消费 M11)。缺省/0 = 旧行为零变化 (即时 boost 路径原样)。显式
    # boost=False 仍纯读零信号 (调用方已自弃强化, 无可改道事件)。
    # refresh_lif_on_recall 函数保留不删 (spec M10 明示); CLI recall 子命令
    # 同语义透传 (env 控制全局, 不加新 flag)。
    delayed = os.environ.get("MEM_DELAYED_REINFORCE", "") == "1"
    if boost and scored and delayed:
        for s in scored:
            signals.append("recall_hits", {
                "fact_id": s["fact"]["id"],
                "session_id": session_id,
                "query": query,
                "score": s["score"],
                "source_cwd": cwd,
            })
    elif boost and scored:
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

    # ADR-4 bfs hint: direct-match 薄(候选 < 阈值)且 use_bfs=False → suggest_bfs。
    # hint 不改 default recall 行为(排序/过滤不变); 字段附在 envelope(verbose/
    # with_tag), 默认 list 路径不动(零回归)。cli 单独从结果数自行判断(不依赖此字段)。
    suggest_bfs = (
        not use_bfs
        and len(candidates) < SUGGEST_BFS_THRESHOLD
    )

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
            "suggest_bfs": suggest_bfs,
            "results": [{"fact": s["fact"], "score": s["score"], "tag": s["tag"]} for s in scored],
        }
    return [s["fact"] for s in scored]


__all__ = ["search_entities", "recall", "bfs_neighbors"]
