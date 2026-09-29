"""T4/P4 语义边发现 — laya 批判新 fact 与 1-hop 邻域的语义关联 (spec v1.1 §四 Step2).

每 fact 一次 laya_batch (每邻居 1 个 4 档 score question, norm /3.0);
>= THRESHOLD 入边。批 None → 该 fact 跳过。开关/可达性: MEM_LAYA_ENABLED
经 laya_available() 短路, 关 → 零网络零边。
"""
from __future__ import annotations

from typing import Any

from laya_client import laya_available, laya_batch, norm_score

THRESHOLD = 0.7
CRITERIA = ["unrelated", "weak", "related", "strong"]  # 4 档, norm /3.0


def _fact_text(f: dict[str, Any]) -> str:
    return f"{f.get('predicate') or ''}: {f.get('value') or ''}".strip()


def discover_semantic_edges(
    new_facts: list[dict[str, Any]],
    neighbors: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """对每条新 fact 与其邻居集跑 laya 批判, 迈阈边列表 (不落库)。

    ``neighbors[fact_id]`` = 该 fact subject 的 1-hop 邻域 active fact
    (调用方经 recall._facts_for_entities 同源查询取)。每 fact 恰 1 次
    laya_batch; 任一批失败 (None) → 该 fact 跳过不炸。
    """
    edges: list[dict[str, Any]] = []
    if not laya_available():
        return edges
    for f in new_facts:
        nbrs = [n for n in neighbors.get(f["id"], []) if n["id"] != f["id"]]
        if not nbrs:
            continue
        state = (f"Fact: {_fact_text(f)}\n"
                 + "\n".join(f"[{n['id']}] {_fact_text(n)}" for n in nbrs))
        questions = {
            n["id"]: {"type": "score",
                      "instructions": f"Semantic relation of [{n['id']}] to the fact?",
                      "criteria": CRITERIA}
            for n in nbrs
        }
        answers = laya_batch(state, questions)
        if answers is None:
            continue
        nbr_ids = {n["id"] for n in nbrs}
        for nid, a in answers.items():
            # review M2: 形态不良跳行(不炸整批); 幻觉 id 拒写
            if nid not in nbr_ids or not isinstance(a, dict) \
                    or not isinstance(a.get("score"), (int, float)):
                continue
            w = norm_score(a, len(CRITERIA))
            if w >= THRESHOLD:
                edges.append({"source_id": f["id"], "target_id": nid,
                              "weight": w, "created_by": "laya"})
    return edges


def discover_and_store(new_fact_ids: list[str],
                       as_of: str | None = None) -> int:
    """autodream 收尾入口: 取 fact + 1-hop 邻域 → discover → put。

    neighbors = recall._facts_for_entities([subject_id]) 同源查询
    (subject OR object 命中的 active fact)。返回写入边数; 任何异常向上抛
    (调用方降级 warn, 不阻断 ingest)。
    """
    if not new_fact_ids:
        return 0
    import recall as recall_mod
    import store
    new_facts = [f for f in (store.get_fact(fid) for fid in new_fact_ids) if f]
    if not new_facts:
        return 0
    neighbors: dict[str, list[dict[str, Any]]] = {
        f["id"]: recall_mod._facts_for_entities([f["subject_id"]], as_of=as_of)
        for f in new_facts
    }
    return store.put_semantic_edges(
        discover_semantic_edges(new_facts, neighbors))
