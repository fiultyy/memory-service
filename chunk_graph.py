"""G1 chunk 抽取管道 (spec: docs/specs/graph-granularity-reform-v1.md §一/§三 G1).

并行新管道, 不动现有抽取主链 (autodream 接线由 orchestrator 后续裁决):
  split_units → pack_units → aggregate_chunks → summarize_chunk → ingest_chunks

原子 = chunk(完整语义段); fact.value = 结论句, predicate='chunk_of'(暂定,
G3 重建时 laya 赋真边型)。answer 消费全部键位守卫 (仓内 3 起键位错读坑史)。
"""
from __future__ import annotations

import re

import store
from laya_client import TOKEN_BUDGET
import laya_client

# 逐句口径与 T3 校准一致 (noise 恒 0.00 / signal 0.29-0.80, 0.4 会误滤)
_FILTER_THRESH = 0.25
_UNIT_RE = re.compile(r"[^。！？；.!?;\n]+[。！？；.!?;]*")


def split_units(text: str) -> list[str]:
    """regex 标点为原子单元切分 (中英句末标点+换行), 保留原文, 丢弃纯空白单元。"""
    return [u for u in (m.group(0).strip() for m in _UNIT_RE.finditer(text)) if u]


def pack_units(units: list[str], budget: int = TOKEN_BUDGET) -> list[list[str]]:
    """按 laya 同口径 (len//4) 估算打包, 单包 ≤budget; 超长单句独占一包。"""
    packs: list[list[str]] = []
    cur: list[str] = []
    cur_tokens = 0
    for u in units:
        t = max(1, len(u) // 4)
        if cur and cur_tokens + t > budget:
            packs.append(cur)
            cur, cur_tokens = [], 0
        cur.append(u)
        cur_tokens += t
    if cur:
        packs.append(cur)
    return packs


def _answer_dict(answers: dict | None, qid: str) -> dict | None:
    """键位守卫: answers/qid/answer 任一非 dict → None (畸形不炸不误读)。"""
    if not isinstance(answers, dict):
        return None
    ans = answers.get(qid)
    return ans if isinstance(ans, dict) else None


def _filter_units(units: list[str]) -> list[str] | None:
    """逐句 1 noul 过滤寒暄/噪声句; state 只含该句 (整批共享会把 noul 洗成常数,
    仓内实测)。任一句守卫不过 → 保守保留该句; laya 不可用 → None (调用方降级全留)。"""
    if not laya_client.laya_available():
        return None
    kept = []
    for i, u in enumerate(units):
        a = laya_client.laya_batch(u, {f"flt_{i}": {"type": "noul", "instructions":
                          "Does this sentence carry a complete standalone "
                          "conclusion worth remembering?"}})
        if a is None:  # 整批 None (laya 抖动) → 组级降级, 与 _cluster_units 同口径
            return None
        ans = _answer_dict(a, f"flt_{i}")
        # 守卫不过(缺 noul/非数值) → 保留 (保守, 与 T3 畸形段不过滤一致)
        if not (isinstance(ans.get("noul"), (int, float))
                and ans["noul"] < _FILTER_THRESH):
            kept.append(u)
    return kept


def _cluster_units(units: list[str]) -> list[list[str]] | None:
    """聚合批: state = 全部候选句逐行; 每句 1 个 choice (criteria=其余每句
    "semantically belongs with unit X"); choice 命中即 union。整批 None → None。"""
    lines = "\n".join(f"[{i}] {u}" for i, u in enumerate(units))
    if len(units) == 1:  # 单元素 criteria={} 会被 laya 拒 → 短路自聚 (G2 534 降级组根因)
        return [units]
    questions = {}
    for i in range(len(units)):
        questions[f"agg_{i}"] = {
            "type": "choice",
            "instructions": "Which other unit belongs to the same semantic "
                            "chunk as this unit?",
            "criteria": {f"{j}": f"semantically belongs with unit {i}"
                         for j in range(len(units)) if j != i}}
    answers = laya_client.laya_batch(f"candidate units:\n{lines}", questions)
    if answers is None:
        return None
    parent = list(range(len(units)))  # union-find

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(units)):
        ans = _answer_dict(answers, f"agg_{i}")
        # 键位守卫: choice 必须是既有单元 id 且置信 ≥0.5 (幻觉 guard, 与 T5 同款)
        if isinstance(ans, dict) and isinstance(ans.get("choice"), str) \
                and isinstance(ans.get("confidence"), (int, float)) \
                and ans["confidence"] >= 0.5 and ans["choice"].isdigit() \
                and int(ans["choice"]) < len(units):
            parent[find(i)] = find(int(ans["choice"]))
    clusters: dict[int, list[str]] = {}
    for i, u in enumerate(units):
        clusters.setdefault(find(i), []).append(u)
    return list(clusters.values())


def aggregate_chunks(pack: list[str]) -> list[dict] | None:
    """一个包: (a) 逐句 noul 过滤 (b) choice 聚簇。任一步 laya 批 None → None
    (该包由调用方处理); 全句被滤 → 空列表。"""
    if not laya_client.laya_available():
        return None
    kept = _filter_units(pack)
    if kept is None:
        return None
    if not kept:
        return []
    clusters = _cluster_units(kept)
    if clusters is None:
        return None
    return [{"text": "".join(units), "units": units} for units in clusters]


def _summary_candidates(chunk_text: str, units: list[str]) -> list[str]:
    """laya 是 System-1 裁判不产文本 → 结论句候选本地构造 (首句/最长句/整段),
    laya 只做选择。"""
    cands = [units[0], max(units, key=len)]
    if len(chunk_text) <= 30:
        cands.append(chunk_text)
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def summarize_chunk(chunk_text: str, units: list[str] | None = None) -> str | None:
    """单 chunk 一次 laya_batch choice 选结论句; 拿不准 → None 调用方降级用原文首句。"""
    units = units if units else split_units(chunk_text) or [chunk_text]
    cands = _summary_candidates(chunk_text, units)
    if len(cands) == 1:
        return cands[0]
    if not laya_client.laya_available():
        return None
    answers = laya_client.laya_batch(
        f"chunk text:\n{chunk_text}",
        {"sum": {"type": "choice", "instructions":
                 "Which candidate is the best standalone conclusion for "
                 "this chunk (concise human-readable, <=30 chars)?",
                 "criteria": {str(i): c for i, c in enumerate(cands)}}})
    ans = _answer_dict(answers, "sum")
    if isinstance(ans, dict) and isinstance(ans.get("choice"), str) \
            and isinstance(ans.get("confidence"), (int, float)) \
            and ans["confidence"] >= 0.5 and ans["choice"].isdigit() \
            and int(ans["choice"]) < len(cands):
        return cands[int(ans["choice"])]
    return None


def _mount_topic(summary: str, chunk_text: str) -> str:
    """挂载点: 结论句向量召回已有 entity top-N, laya choice 选主; 无候选/不中
    → 新建占位 topic entity。"""
    import embedding
    import resolver
    emb = embedding.embed(summary)
    cands = resolver._cosine_topk(emb, 5) if emb else []
    if cands and laya_client.laya_available():
        answers = laya_client.laya_batch(
            f"chunk conclusion: {summary}\nchunk text:\n{chunk_text}",
            {"mount": {"type": "choice", "instructions":
                       "Which existing entity does this chunk mainly discuss?",
                       "criteria": {c["id"]: f"entity: {c['name']} "
                                             f"({c['type']})" for c in cands}}})
        ans = _answer_dict(answers, "mount")
        if isinstance(ans, dict) and isinstance(ans.get("choice"), str) \
                and isinstance(ans.get("confidence"), (int, float)) \
                and ans["confidence"] >= 0.5 \
                and ans["choice"] in {c["id"] for c in cands}:
            return ans["choice"]
    return store.put_entity(summary[:40], "topic", name_embedding=emb)


def ingest_chunks(chunks: list[dict], source_cwd: str | None,
                  session_id: str | None = None) -> list[str]:
    """每 chunk put_fact: value=结论句, predicate='chunk_of', subject=挂载点。
    结论句 embedding 由 put_fact 现有通道自动预热 (store.put_fact 内嵌
    embedding.embed + vec_index.sync_fact)。"""
    fids = []
    for ch in chunks:
        units = ch.get("units") or split_units(ch["text"])
        summary = summarize_chunk(ch["text"], units) or units[0]
        subject = _mount_topic(summary, ch["text"])
        fids.append(store.put_fact(
            subject, "chunk_of", summary,
            extractor="chunk_graph",
            source_refs=[u for u in units],
            source_cwd=source_cwd,
            seen_sessions=[session_id] if session_id else [],
            topic=summary))
    return fids
