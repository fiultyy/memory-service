"""mem-service M9 surprise 计算 — 升级队列的优先级源 (P26 双轴 / P29 / D8).

入队时算 (upgrade.enqueue 内调用), 写 ``surprise`` 复合值 + ``priority`` 列。
两路信号 (原三路; 实体型惊喜轴已删, 见下):

- **novelty (主分量, 内容轴)**: 候选文本 embedding 与既有 active facts
  ``value`` 向量的 ``1 − max cosine`` (embedding.py L1/L2 双缓存基建复用 —
  fact value 向量不落库, 逐次 embed 走缓存, recall.py 同惯例)。embedding
  离线返回 [] / 抛错 → novelty 记 None (不可考), priority 记 0 — 降级不
  crash, 循 resolver 红线惯例。
- **结构型惊喜 (加成项)**: 谓词在既有谓词表 (KG DISTINCT predicate ∪
  extractor 关系模式表) 之外 → 新关系类型 = 结构轴扩张 (P26 双轴之二)。

实体型惊喜 (原加成项) 已删除 (graph-reform v2 裁决#4, 2026-10-01): 该特征
= gazetteer 词典 miss 比例, 正是被退役的 dict 式硬编码过滤类信号; 改造为
embedding 版需逐入队对全实体表算覆盖, 为 0.2 权重的加成项不成比例 — 取
「降权删除」分支。新实体边界扩张信号由 ingest 主径 (zhipu v4 判据) 承担。

合成式 (novelty 内容轴主导, 结构轴加成):

    surprise = clamp(novelty + W_STRUCT × structural)
    priority = |surprise|^α      (D8 唯一采纳的采样公式; α 缺省 1.0 可调)

novelty 不可考 (离线) → surprise=None, priority=0 (排队但零优先, 人工/后续
重算可救)。α≠1 时 priority 单调保序 (|·|^α 对非负 surprise 单调)。
"""

from __future__ import annotations

from typing import Any

import db
import embedding
import extractor
from recall import _cosine  # 纯 Python cosine, 0.0 on empty/zero-norm

# priority = |surprise|^α 的指数 (D8 采样公式唯一采纳项; 可调: >1 偏头部,
# <1 拉平长尾)。
_ALPHA = 1.0
# 加成权重 (P26: novelty 主导, 结构惊喜作加成不作主分量)。
_STRUCT_WEIGHT = 0.2
# 复合值上限 (novelty≤1 + 加成≤0.2 → 1.2; clamp 保标量语义稳定)。
_MAX_SURPRISE = 1.2


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


def novelty_sample(text: str) -> str:
    """novelty embedding 的采样文本: 前缀截断 (perf/vec-index)。

    段全文可达数千字符 (101 库 p90=4000) — novelty 是优先级启发信号而非
    语义精确度量, 全文 embed 的 GPU 代价 (~390k chars/全量 init) 不成比例;
    前缀 150 字符 (全量 init 段样本 84k→21k chars), 截断只影响超长段 (采样代表性充分)。测试
    文本均短 → 零影响。
    """
    return text[:_NOVELTY_TEXT_CAP]


_NOVELTY_TEXT_CAP = 150


def _novelty(text: str) -> float | None:
    """1 − max cosine(候选采样, 既有 active fact value 向量)。离线 → None。

    perf/vec-index: 主路径走 vec_fact ANN (单查询取 top-1 = max cosine,
    替代逐 fact embed+Python 余弦扫描 — 千 fact 级扫描 O(N) 字典+余弦)。
    vec_fact 为空 (测试 fake 向量维度不匹配 / 全库离线创建) → 回退旧扫描
    路径 (语义同旧实现)。ANN 集合不含 ingest 时 embed 失败的 fact →
    max 可能略低 → novelty 略高 (更保守的入队优先级, 可接受漂移)。
    """
    try:
        vec = embedding.embed(novelty_sample(text))
    except Exception:
        vec = []
    if not vec:
        return None
    try:
        import vec_index
        top = vec_index.fact_topk(vec, 1)
    except Exception:
        top = []
    if top:
        return 1.0 - top[0][1]
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


def compute(text: str, *, predicates: tuple[str, ...] = (),
            entities: tuple[str, ...] = ()) -> dict[str, Any]:
    """M9 复合惊喜 + 优先级 (实体 miss 轴已删, 裁决#4 2026-10-01 — 见模块
    docstring; ``entities`` 形参保留仅为 upgrade.enqueue 调用兼容, 忽略)。

    Args:
        text: 候选素材文本 (段全文 / fact 三元组拼接)。
        predicates: 素材携带的谓词 (fact 入队点传; 段入队点空) — 任一表外
            → 结构型惊喜。
        entities: 忽略 (兼容保留)。

    Returns:
        ``{"novelty", "structural", "surprise", "priority"}``;
        novelty None (embedding 离线) ⇒ surprise None, priority 0.0。
    """
    novelty = _novelty(text)
    known = known_predicates()
    structural = any(p and p not in known for p in predicates)
    if novelty is None:
        return {"novelty": None, "structural": structural,
                "surprise": None, "priority": 0.0}
    surprise = min(_MAX_SURPRISE,
                   novelty + _STRUCT_WEIGHT * (1.0 if structural else 0.0))
    priority = abs(surprise) ** _ALPHA
    return {"novelty": novelty, "structural": structural,
            "surprise": surprise, "priority": priority}
