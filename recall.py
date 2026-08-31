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
import gate
import networkx as nx
import projection
import scoring
import signals
import store

# ADR-13 向量层(use_vec): 候选扩展 + vec_sim 阈值/top-N。
# ADR-4 噪音地板: score < 此值的 fact 视为噪音丢弃 (BFS 扩展 fact 绕过 —
# 见 use_bfs)。默认为**短 query** (交互式 recall, "rust" / "sqlite-vec 部署")
# 校准: match_item 是 query token 命中率, 长 prompt (harness P2 注入, 整段
# 用户 prompt 作 query) token 数多 → 命中率被稀释 → 强相关 fact 也只有
# ~0.19。注入通道用 min_score 参数自校准低门槛, 默认语义零变化。
SCORE_FLOOR = 0.3

# v1.7③ M3 终裁 (门槛解耦 — 软惩罚, 显式推翻 M13 G5 硬门): BFS 扩展通道
# (B 翼) 不再按 lif_source 硬拒入场 —— 低档 fact 仍经扩展入场 (独有边不因
# 档位丢失), 但排序分乘 gate_mod = 0.5 + 0.5·min(1, lif_source/0.7)
# (regex 0.4 档 ≈0.786 折, llm 0.7/vote 0.85/human 0.9 档恒 1.0)。
# `_BFS_SOURCE_GATE` 保留为公式分母。乘子只挂 b_wing_ids (经 BFS 扩展口
# 入场的专属集), 不进 score_fact、不挂 bfs_expanded_ids (主路径/向量路蹭标
# 会泄漏乘子); 折后分仍绕 SCORE_FLOOR 入榜 (绕地板键仍是 bfs_expanded_ids),
# 仅排序降权。A 路永远注入、不受乘子影响。[设] 可调。
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
    min_score: float | None = None,
    use_gate: bool = False,
    gate_account: bool = True,
    gate_provider: Any = None,
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
        min_score: 噪音地板覆盖 (default None ⇒ :data:`SCORE_FLOOR` 0.3)。
            长 prompt 查询场景 (harness P2 注入) 的 match 稀释自校准用;
            交互式短 query 语义零变化。BFS 扩展 fact 仍绕过任何地板值。
        session_id: Session doing the recall (drives lif_spread on boost).
        boost: Refresh LIF on hit facts (ADR-8v2 reinforcement); default True.
        weights: Optional ``(α, β, γ)`` override forwarded to ``score_fact``
            (ADR-4v2 调参); None ⇒ module defaults. eval_recall grid passes
            candidate triples here.
        use_gate: v1.7③ — 对 b_wing_ids 成员 (BFS 扩展口入场的 B 翼) 跑单 LLM
            窄域 gate (gate.py)。判 keep 的 fact 附 ``gate_keep=True`` +
            ``match_score`` 键; 判不匹配 / gate 不可用 (ProviderCallError/超时/
            两轮 schema 败/断供短路) → **B 翼 fact 全部不入返回** (只注入 A,
            spec §③ 失败语义; 不降级不静默当 keep), recall 不炸。keep 的
            fact 按 N2 语义累计 ``gate_score`` (见 ``gate_account``)。A 路
            fact 永不经 gate、不带这两个键。
        gate_account: gate_score 解锁累计记账开关 (N2, 暂缓期只写不读)。
            True (默认, 注入面首轮档) → keep 的 match_score 经
            :func:`store.bump_gate_score` 求和封顶入账; False (CLI 手动
            --gate 面) → **不入解锁累计** (v7 三句之三: 防 CLI 探测污染账本),
            gate 判定与输出 schema 仍零分叉。
        gate_provider: gate LLM 依赖注入 seam (照 ``llm_extract.extract``
            provider= 先例; 测试零网络注入 mock)。None ⇒ gate 内部按断供
            红线自检 (env 无 ZHIPU_API_KEY 直接短路"无 gate")。

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

    # ADR-13 向量候选扩展(use_vec): query embed 一次 → vec_fact ANN top-N
    # (perf/vec-index: 替代逐 fact 重 embed+Python 余弦; cosine 度量语义等价)。
    # embedding passive([]); use_vec 且 qv 空时跳过(回退纯字面/centrality/LIF)。
    qv = embedding.embed(query) if use_vec else []
    if qv:
        import vec_index
        vec_cands: list[tuple[dict[str, Any], float]] = []
        ann = vec_index.fact_topk(qv, VEC_TOP_N * 3)  # 多取兜 active 过滤
        rows_by_id = {}
        if ann:
            ph = ",".join("?" * len(ann))
            vrows = conn.execute(
                f"SELECT * FROM fact WHERE id IN ({ph}) AND status = 'active'",
                [fid for fid, _ in ann]).fetchall()
            rows_by_id = {r["id"]: r for r in vrows}
        for fid, sim in ann:
            r = rows_by_id.get(fid)
            if r is None or fid in seen_ids:
                continue  # 非活跃 (vec 行残留双保险) / 已入候选
            if sim >= VEC_MIN:
                vec_cands.append((store._decode_fact(r), sim))
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
    b_wing_ids: set[str] = set()
    if use_bfs and entities:
        seed_ids = [e["id"] for e in entities]
        bfs_result = bfs_neighbors(seed_ids, entity_graph, hops=bfs_hops)
        # 邻居实体(非 seed) → 取回 fact 扩展候选集
        neighbor_eids = [eid for eid in bfs_result if eid not in seed_ids]
        if neighbor_eids:
            for f in _facts_for_entities(neighbor_eids, as_of=as_of):
                # v1.7③ M3 软惩罚化: 原 M13 硬门 (lif_source < 0.7 → continue)
                # 摘除 — 低档邻居 fact 仍入场, 排序降权在 scored 构建后统一乘
                # gate_mod (见下)。lif_source 读取保留, 移至乘子处取。
                fid = f["id"]
                if fid not in seen_ids:
                    seen_ids.add(fid)
                    candidates.append(f)
                    # B 翼专属集 (v1.7③): 仅经本口入场的 fact —— gate_mod 只乘
                    # 此集, gate 只判此集; 不挂 bfs_expanded_ids (主路径 fact 端点
                    # 恰为邻居时也被打标, 不纯, 乘子泄漏会污染 A 路)。
                    b_wing_ids.add(fid)
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
    # perf/vec-index: 候选 vec_sim 批量化 — 一次 vec0 全量 MATCH (C 端扫描,
    # 千行 ~20ms) 取全部 fact 距离映射, 替代逐候选 Python _cosine (千 fact
    # 字面候选 ~1.7s)。vec0 点查 (fact_id IN) 实测 ~2ms/行不可用。无 vec
    # 行的候选 (ingest 时 embed 失败) → sim 0 (同旧 fv 空语义)。embedding
    # 离线 qv=[] 时整块跳过 (同旧)。规模注: LIMIT=表行数 → 全扫描, MVP
    # 单机千~万 fact 量级设计 (与字面线性扫描天花板同量级)。
    sim_by_id: dict[str, float] = {}
    if qv:
        import vec_index
        conn = db.get_conn()
        total = conn.execute("SELECT COUNT(*) FROM vec_fact").fetchone()[0]
        if total:
            for fid, sim in vec_index.fact_topk(qv, total):
                sim_by_id[fid] = sim
    scored = []
    for f in candidates:
        vs = sim_by_id.get(f["id"], 0.0)
        bfs_prox = _hop_decay(bfs_fact_min_hop.get(f["id"], -1)) if use_bfs else 0.0
        scored.append(scoring.score_fact(
            f, query,
            centrality=_fact_centrality(f, centralities),
            vec_sim=vs, weights=weights, delta=delta,
            bfs_proximity=bfs_prox,
        ))
    # v1.7③ M3 软惩罚(无条件, 与 gate 是否在场无关): b_wing_ids 成员排序分乘
    # gate_mod = 0.5 + 0.5·min(1, lif_source/_BFS_SOURCE_GATE) (regex 0.4 档
    # ≈0.786 折, ≥0.7 档恒 1.0)。只乘 B 翼专属集 — 不进 score_fact(A 路分数
    # 逐字不动)、不挂 bfs_expanded_ids(主路径蹭标会泄漏乘子); 折后分仍绕
    # SCORE_FLOOR 入榜(绕地板键仍是 bfs_expanded_ids), 仅排序降权。
    for s in scored:
        if s["fact"]["id"] in b_wing_ids:
            lif_source = float(s["fact"].get("lif_source") or 0.0)
            s["score"] *= 0.5 + 0.5 * min(1.0, lif_source / _BFS_SOURCE_GATE)
    # drop low-score (噪音); BFS 扩展 fact(hop>0) 绕过地板但仍参与排序+top_k。
    floor = SCORE_FLOOR if min_score is None else min_score
    scored = [s for s in scored if s["score"] >= floor or s["fact"]["id"] in bfs_expanded_ids]
    # v1.7③ 单 LLM gate(use_gate): 只判 b_wing_ids 成员(B 翼)。keep → fact 附
    # gate_keep=True + match_score 键, 并按 gate_account 累计 gate_score(N2);
    # 判不匹配 → 该条剔除; gate 不可用(断供短路/ProviderCallError/超时/两轮
    # schema 败, gate.GateFailed 响亮上抛在此承接) → **B 翼全部不入返回, 只注入
    # A** — 不降级、不静默当 keep, recall 不炸(spec §③ 失败语义)。
    if use_gate and b_wing_ids:
        cand_texts: dict[str, str] = {}
        for s in scored:
            f = s["fact"]
            if f["id"] in b_wing_ids:
                cand_texts[f["id"]] = (
                    (f.get("value") or "") + "\n" + (f.get("topic") or "")
                ).strip()
        try:
            verdicts = gate.run_gate(
                cand_texts, query,
                provider=gate_provider,
                scope=("manual" if not gate_account else "recall"),
            )
        except Exception:  # noqa: BLE001 — gate 任何失败 ≡ 不可用, 契约只要求 A 路
            verdicts = None
        if verdicts is None:
            scored = [s for s in scored if s["fact"]["id"] not in b_wing_ids]
        else:
            kept: list[dict[str, Any]] = []
            acct_conn = db.get_conn() if gate_account else None
            for s in scored:
                fid = s["fact"]["id"]
                if fid not in b_wing_ids:
                    kept.append(s)  # A 路: 永不经 gate, 不带 gate 键
                    continue
                v = verdicts.get(fid)
                if v is not None and v.get("keep"):
                    s["fact"]["gate_keep"] = True
                    s["fact"]["match_score"] = float(v["match_score"])
                    kept.append(s)
                    if gate_account:
                        # N2: 求和且达解锁阈值封顶(MEM_UNLOCK_MATCH_SCORE, 默认
                        # 2.0); 暂缓期只写不读。手动面 gate_account=False 不入账。
                        store.bump_gate_score(fid, float(v["match_score"]),
                                              conn=acct_conn)
            scored = kept
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
    # 只建散 index 载体, 不碰 MEMORY.md — 投影索引统一归 synthesis_index 唯一写入口 (09-01 终裁A方案: SessionStart 单点自动投影), 防双写竞争。mem_dir 优先显式, 否则 cwd 推导;
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
