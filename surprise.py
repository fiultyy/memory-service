"""mem-service M9 surprise 计算 — 升级队列的优先级源 (P26 双轴 / P29 / D8).

入队时算 (upgrade.enqueue 内调用), 写 ``surprise`` 复合值 + ``priority`` 列。
三路信号:

- **novelty (主分量, 内容轴)**: 候选文本 embedding 与既有 active facts
  ``value`` 向量的 ``1 − max cosine`` (embedding.py L1/L2 双缓存基建复用 —
  fact value 向量不落库, 逐次 embed 走缓存, recall.py 同惯例)。embedding
  离线返回 [] / 抛错 → novelty 记 None (不可考), priority 记 0 — 降级不
  crash, 循 resolver 红线惯例。
- **实体型惊喜 (加成项)**: gazetteer miss — 大写词实体候选 (API 调用方亦可
  显式传 subject/object 名) 既不在 KG 词典 (entity.name+aliases) 也不被
  extractor regex 模式覆盖 → miss 比例。新实体 = 知识边界扩张信号 (P26)。
- **结构型惊喜 (加成项)**: 谓词在既有谓词表 (KG DISTINCT predicate ∪
  extractor 关系模式表) 之外 → 新关系类型 = 结构轴扩张 (P26 双轴之二)。

合成式 (施工自定, 依据 P26 双轴 — novelty 内容轴主导, 实体/结构轴加成):

    surprise = clamp(novelty + W_ENT × entity_miss_ratio + W_STRUCT × structural)
    priority = |surprise|^α      (D8 唯一采纳的采样公式; α 缺省 1.0 可调)

novelty 不可考 (离线) → surprise=None, priority=0 (排队但零优先, 人工/后续
重算可救)。α≠1 时 priority 单调保序 (|·|^α 对非负 surprise 单调)。
"""

from __future__ import annotations

import re
from typing import Any

import db
import embedding
import extractor
from recall import _cosine  # 纯 Python cosine, 0.0 on empty/zero-norm

# priority = |surprise|^α 的指数 (D8 采样公式唯一采纳项; 可调: >1 偏头部,
# <1 拉平长尾)。
_ALPHA = 1.0
# 双轴加成权重 (P26: novelty 主导, 实体/结构惊喜作加成不作主分量)。
_ENTITY_WEIGHT = 0.2
_STRUCT_WEIGHT = 0.2
# 复合值上限 (novelty≤1 + 加成≤0.4 → 1.4; clamp 到 1.4 保持标量语义稳定)。
_MAX_SURPRISE = 1.4

# 实体候选启发: 独立大写词 (新专名的最弱信号; 词典+regex 覆盖检查在此候选集上)。
_CAP_WORD = re.compile(r"\b[A-Z][a-z]{2,}\b")


def known_predicates() -> set[str]:
    """既有谓词表 = extractor 关系模式表 ∪ KG DISTINCT predicate (结构轴基准)。"""
    preds = {p for _, p in extractor._RELATION_PATTERNS}
    preds |= {p for _, p in extractor._CJK_RELATION_PATTERNS}
    try:
        conn = db.get_conn()
        for row in conn.execute("SELECT DISTINCT predicate FROM fact"):
            preds.add(row[0])
    except Exception:
        pass  # 未 init / 表缺失 → 模式表兜底
    return preds


def _novelty(text: str) -> float | None:
    """1 − max cosine(候选, 既有 active fact value 向量)。离线 → None。"""
    try:
        vec = embedding.embed(text)
    except Exception:
        vec = []
    if not vec:
        return None
    try:
        conn = db.get_conn()
        rows = conn.execute(
            "SELECT value FROM fact WHERE status='active' AND value IS NOT NULL"
        ).fetchall()
    except Exception:
        return None
    best = 0.0
    for row in rows:
        try:
            fv = embedding.embed(row[0])  # L1/L2 缓存 (recall.py 同惯例)
        except Exception:
            continue
        sim = _cosine(vec, fv)
        if sim > best:
            best = sim
    return 1.0 - best


def _entity_miss_ratio(text: str, candidate_entities: tuple[str, ...] = ()) -> float:
    """gazetteer miss 比例: 实体候选 (显式传入 ∪ 大写词启发) 中既不在 KG
    词典、也不被 extractor regex 实体模式覆盖的比例。无候选 → 0。"""
    candidates = {c.strip() for c in candidate_entities if c and c.strip()}
    candidates |= set(_CAP_WORD.findall(text))
    if not candidates:
        return 0.0
    from gazetteer import _load_gazetteer  # 惰性 import 防环 (gazetteer → db)
    covered = {s.lower() for s in _load_gazetteer()}
    covered |= {e["name"].lower()
                for e in extractor._extract_entities(text)}
    misses = [c for c in candidates if c.lower() not in covered]
    return len(misses) / len(candidates)


def compute(text: str, *, predicates: tuple[str, ...] = (),
            entities: tuple[str, ...] = ()) -> dict[str, Any]:
    """M9 复合惊喜 + 优先级。

    Args:
        text: 候选素材文本 (段全文 / fact 三元组拼接)。
        predicates: 素材携带的谓词 (fact 入队点传; 段入队点空) — 任一表外
            → 结构型惊喜。
        entities: 素材携带的实体名 (fact 入队点传 subject/object; 段入队点
            空 — 大写词启发兜底)。

    Returns:
        ``{"novelty", "entity_miss", "structural", "surprise", "priority"}``;
        novelty None (embedding 离线) ⇒ surprise None, priority 0.0。
    """
    novelty = _novelty(text)
    entity_miss = _entity_miss_ratio(text, entities)
    known = known_predicates()
    structural = any(p and p not in known for p in predicates)
    if novelty is None:
        return {"novelty": None, "entity_miss": entity_miss,
                "structural": structural, "surprise": None, "priority": 0.0}
    surprise = min(_MAX_SURPRISE,
                   novelty + _ENTITY_WEIGHT * entity_miss
                   + _STRUCT_WEIGHT * (1.0 if structural else 0.0))
    priority = abs(surprise) ** _ALPHA
    return {"novelty": novelty, "entity_miss": entity_miss,
            "structural": structural, "surprise": surprise,
            "priority": priority}
