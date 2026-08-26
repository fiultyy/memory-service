"""谓词聚边 (batch 13, 用户裁决 2026-08-27: 开放谓词词汇 + 近似度聚类 + 词频统计)。

LLM 直抽后谓词是**开放词汇** (12 门枚举已撤, 用户指令「开放」/「放掉按
LLM提」); 本模块把 raw 谓词按向量近似度聚到 canonical 代表:

- ``fact.predicate``   存聚类后 canonical (代表词)
- ``fact.raw_predicate`` 存 LLM 原文 (审计/统计)
- ``predicate_registry`` (db 表) 累积 canonical 计数 + embedding — 即
  「后续按近似度阈值进行词频统计」的持续机制, 任意时点可查分布。

聚类协议 (每轮 dream):
1. 批内 raw 去重 → 一次 ``embed_batch`` (LM Studio 本地, L1 缓存命中后零开销)
2. 顺序对 registry 余弦最近: ≥ 阈值 (env ``MEM_PRED_CLUSTER_THRESHOLD``,
   默认 0.75) → 归既有 canonical; 否则 raw 自成新 canonical
3. 计数按出现次数累加, 持久化 upsert

位置: autodream 段提取完成后、Phase c 增量决策前 (去重/supersede 比较必须
发生在 canonical 上, 否则同义异名谓词一跑内互躲去重) — 即「最后」的提取后
步骤 / 第一个持久化前步骤。
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone

import db


DEFAULT_THRESHOLD = 0.75


def _threshold() -> float:
    """聚类阈值 (env MEM_PRED_CLUSTER_THRESHOLD 可调, 默认 0.75)。"""
    try:
        return float(os.environ.get("MEM_PRED_CLUSTER_THRESHOLD",
                                    DEFAULT_THRESHOLD))
    except ValueError:
        return DEFAULT_THRESHOLD


def _cos(a: list[float], b: list[float]) -> float:
    """余弦相似度 (维度不齐/零向量 → 0.0, 不抛 — registry 脏数据防御)。"""
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    dot = sum(x * y for x, y in zip(a[:n], b[:n]))
    na = math.sqrt(sum(x * x for x in a[:n]))
    nb = math.sqrt(sum(x * x for x in b[:n]))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _load_registry() -> dict[str, tuple[int, list[float]]]:
    """canonical → (count, vec)。embedding 存 JSON list (同 entity.name_embedding 惯例)。"""
    out: dict[str, tuple[int, list[float]]] = {}
    for row in db.get_conn().execute(
            "SELECT canonical, count, embedding FROM predicate_registry"):
        try:
            vec = json.loads(row["embedding"])
        except (json.JSONDecodeError, TypeError):
            continue  # 脏行跳过 (不并也不炸聚类)
        out[row["canonical"]] = (int(row["count"]), vec)
    return out


def _persist(rows: dict[str, tuple[int, list[float]]]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = db.get_conn()
    conn.executemany(
        """INSERT INTO predicate_registry (canonical, count, embedding, updated_at)
           VALUES (?,?,?,?)
           ON CONFLICT(canonical) DO UPDATE SET
               count=excluded.count, embedding=excluded.embedding,
               updated_at=excluded.updated_at""",
        [(c, v[0], json.dumps(v[1], ensure_ascii=False), now)
         for c, v in rows.items()])


def cluster(raws: list[str],
            vectors: list[list[float]] | None = None) -> dict[str, str]:
    """raw 谓词列表 → ``{raw: canonical}`` 映射 (含注册表更新)。

    Args:
        raws: 本轮全部边上的 raw 谓词 (含重复 — 重复即词频)。
        vectors: 测试注入的预计算向量 (缺省 ``embedding.embed_batch``,
            本地 LM Studio + L1 缓存)。

    同批内近义也并 (顺序处理, 先到者成 canonical); 与 registry 既有
    canonical 近义 → 归既有 (即使本批首个 raw 与其字面不同)。
    """
    # 批内去重保序 + 词频
    counts: dict[str, int] = {}
    for r in raws:
        r = (r or "").strip()
        if r:
            counts[r] = counts.get(r, 0) + 1
    if not counts:
        return {}

    uniq = list(counts.keys())
    if vectors is None:
        import embedding
        vectors = embedding.embed_batch(uniq)
    if len(vectors) != len(uniq):  # 防御: embed 缺行 → 逐 raw 自成 canonical
        vectors = [[0.0]] * len(uniq)  # 零向量 cosine=0 → 永不并 (安全侧)

    reg = _load_registry()
    mapping: dict[str, str] = {}
    for raw, vec in zip(uniq, vectors):
        best_canon, best_sim = None, -1.0
        for canon, (_cnt, cvec) in reg.items():
            sim = _cos(vec, cvec)
            if sim > best_sim:
                best_canon, best_sim = canon, sim
        if best_canon is not None and best_sim >= _threshold():
            c, v = reg[best_canon]
            reg[best_canon] = (c + counts[raw], v)
            mapping[raw] = best_canon
        else:
            reg[raw] = (counts[raw], vec)
            mapping[raw] = raw

    _persist(reg)
    return mapping


def stats() -> list[dict]:
    """词频统计 (按 canonical 降序): canonical / count / 成员 raw 分布。"""
    conn = db.get_conn()
    out: list[dict] = []
    for row in conn.execute(
            "SELECT canonical, count FROM predicate_registry "
            "ORDER BY count DESC, canonical"):
        members = conn.execute(
            "SELECT raw_predicate, COUNT(*) n FROM fact "
            "WHERE predicate = ? AND raw_predicate IS NOT NULL "
            "GROUP BY raw_predicate ORDER BY n DESC",
            (row["canonical"],)).fetchall()
        out.append({"canonical": row["canonical"], "count": row["count"],
                    "members": {m["raw_predicate"]: m["n"] for m in members}})
    return out


__all__ = ["cluster", "stats", "DEFAULT_THRESHOLD"]
