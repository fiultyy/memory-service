"""mem-service gazetteer — M7 占位提取器 (KG 自举词典 + extractor regex 三路并行).

D6a/P18/P16/P20 (spec v2 §2 M7): 占位-升级时序的占位半边。与 wings(adapter)
同构输出 (``llm_provider.Extraction``), ``extractor_label='regex'`` →
SOURCE_WEIGHT 0.4 档 (put_fact 自动, 不需新代码)。

三路并行 (deterministic, 零 LLM / 零网络):
  ① 词典匹配: KG ``entity.name + aliases`` 精确命中 → canonical 名 (命中即链
     到既有 entity —— autodream 经 resolver 既有三步的 step1
     ``find_entity_exact`` (name+alias 大小写不敏感) 命中, 不新建重复实体;
     §7 冻结 resolver 不改, 本模块只产出 canonical 名供其链接)。
  ② ``extractor.py`` 休眠 regex 实体模式复活 (§7 冻结指模式资产, 复活调用
     不违冻结)。
  ③ ``extractor.py`` 关系模式 (7 EN 谓词 + CJK 同义集) → 三元组 (P19 输出
     本体: subject/predicate/object, wings 同构)。

块分流 A 前置 (P20 / adapter._ENTITY_QUALITY 精神): fenced code block 与
明显代码行 (shell 命令 / import·def 等代码前缀 / 路径 / URL / 赋值行) 内的
词典命中与 regex 匹配被弃 —— 防代码/工具输出噪声直接变实体。启发式 mask,
保守可调 (ponytail: 线级启发, 非语义判定)。

Coverage ceiling 继承 extractor: 纯中文裸句零命中 (语义事实 defer 到 wings
异步升级侧, M4 队列消费)。
"""

from __future__ import annotations

import json
import re
from typing import Any

import db
import extractor
from llm_provider import EdgeOut, EntityOut, Extraction

# 词典容量闸 (ponytail: 单机 MVP 上限; KG 自举越用越准, 超额截断最旧语义由
# M4 升级侧接管)。SELECT ... ORDER BY created_at 保证截断确定性。
MAX_GAZETTEER_SURFACES = 20000

# 占位档固定 confidence: 确定性提取 (词典/regex 命中即真), 非 LLM 投票聚合;
# 信任分层由 extractor_label='regex' → lif_source=0.4 承载, confidence 不承载。
_PLACEHOLDER_CONFIDENCE = 0.6

# ── 块分流 A: 代码区 mask (fenced block + 明显代码行) ─────────────────

_FENCE_MARKER = re.compile(r"^\s*(```|~~~)")
_CODE_PREFIX = re.compile(
    r"""^\s*(?:
        (?:git|cargo|npm|pnpm|yarn|pip|python3?|node|make|docker|sudo|apt|brew|
          curl|wget|cd|ls|cat|echo|export|rm|mv|cp|mkdir|touch|grep|sed|awk)\b
      | (?:import|from|def|class|fn|func|const|let|var|struct|impl|enum|
          package|use|pub|return|if|for|while|switch|match)\b
      | \#!|/\*|//
    )""",
    re.VERBOSE,
)
_PATHISH = re.compile(
    r"\b[\w.-]+\.(?:py|rs|js|ts|tsx|jsx|json|toml|yaml|yml|db|sqlite3?|sql|"
    r"sh|bash|zsh|go|java|c|cpp|h|hpp|lock|log)\b"
)
_URLISH = re.compile(r"\w+://\S+")
_ASSIGNISH = re.compile(r"\S+\s*=\s*\S+")  # 赋值行 (prose 罕见含 =)

# 实体禁区补充: env 变量名形态 (adapter._ENV_PATTERN 同语义副本, 自包含不引
# wings 模块 — adapter 冻结零依赖)。
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*_[A-Z0-9_]+$")


def _mask_code_zones(text: str) -> str:
    """Blank fenced code blocks & code-like lines (same shape, non-word → space).

    保留换行与缩进形状 (行数/列位不变, 便于未来 span 归因); 块分流 A: 被mask
    区域内的词典命中与 regex 匹配自然落空。
    """
    out_lines: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_MARKER.match(line):
            in_fence = not in_fence
            out_lines.append(_blank(line))
            continue
        if in_fence or _CODE_PREFIX.match(line) or _PATHISH.search(line) \
                or _URLISH.search(line) or _ASSIGNISH.search(line):
            out_lines.append(_blank(line))
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def _blank(line: str) -> str:
    return "".join(ch if ch.isspace() else " " for ch in line)


# ── ① 词典源: KG entity.name + aliases ───────────────────────────────

def _load_gazetteer() -> dict[str, tuple[str, str]]:
    """surface(lower) → (canonical_name, entity_type)。空 KG / 未 init → {}。

    ponytail: 每次调用全量加载 (单机 KG ≤ 1e5, MVP 可承受; 越用越准的代价)。
    """
    try:
        conn = db.get_conn()
        rows = conn.execute(
            "SELECT name, entity_type, aliases FROM entity ORDER BY created_at"
        ).fetchall()
    except Exception:
        return {}
    gaz: dict[str, tuple[str, str]] = {}
    for row in rows:
        canonical, etype = row["name"], row["entity_type"]
        gaz.setdefault(canonical.strip().lower(), (canonical, etype))
        if len(gaz) >= MAX_GAZETTEER_SURFACES:
            break
        try:
            aliases = json.loads(row["aliases"]) if row["aliases"] else []
        except (json.JSONDecodeError, TypeError):
            aliases = []
        for alias in aliases:
            a = (alias or "").strip().lower()
            if len(a) >= 2 and a not in gaz:
                gaz[a] = (canonical, etype)
                if len(gaz) >= MAX_GAZETTEER_SURFACES:
                    break
    return gaz


def _dict_entity_hits(masked: str, gaz: dict[str, tuple[str, str]]
                      ) -> dict[str, EntityOut]:
    """词典命中 → canonical EntityOut (别名 surface 折入 aliases 供 resolver
    step1 命中)。同 canonical 多 surface 命中只出一个实体。"""
    hits: dict[str, EntityOut] = {}
    for surface, (canonical, etype) in gaz.items():
        if len(surface) < 2 or not re.search(
                rf"(?<![\w一-龥]){re.escape(surface)}(?![\w一-龥])",
                masked, re.IGNORECASE):
            continue
        if canonical in hits:
            hits[canonical].aliases.append(surface)  # 归一 surface 供参考
        else:
            hits[canonical] = EntityOut(
                name=canonical, type=etype, aliases=[surface])
    return hits


# ── ①+ 语义优先实体匹配 (追加任务 B: 词典①路升级为表面+语义双路) ────

# CJK 候选 span: CJK 字符 run (标点/空白切分), 长 2–12 字。
_CJK_RUN = re.compile(r"[一-龥]{2,12}")

# 语义链接阈值 (用户裁决实测: 跨语言同义对 cosine 0.53–0.81, 无关对照
# 0.31–0.34, 分离带清晰 → 0.45 可用; [设] 可调)。
_SEMANTIC_LINK_THRESHOLD = 0.45

# 语义候选容量闸 (单段 span 上限; ponytail MVP)。
_MAX_SEMANTIC_SPANS = 16


def _semantic_entity_hits(masked: str, gaz: dict[str, tuple[str, str]],
                          dict_hits: dict[str, EntityOut]
                          ) -> dict[str, str]:
    """B 路触发位: CJK 候选 span (词典未覆盖) → 共用管道语义链接。

    返回 ``{span_lower: canonical}`` 映射 (供 ②③ 路归一, 防关系路把已
    语义链接的 span 原文再声明为新实体)。与 step1/词典表面精确匹配
    **并行不替代**: 词典命中的 span 不重算 (零成本优先)。
    """
    seen_lower = {a.lower() for e in dict_hits.values() for a in e.aliases}
    seen_lower |= {e.name.lower() for e in dict_hits.values()}
    spans: list[str] = []
    for m in _CJK_RUN.finditer(masked):
        s = m.group(0)
        if s.lower() in seen_lower or s.lower() in gaz:
            continue  # 词典/已命中 surface 精确覆盖 → 语义路不重算
        if s not in spans:
            spans.append(s)
        if len(spans) >= _MAX_SEMANTIC_SPANS:
            break
    return _link_spans(spans, dict_hits)


def _link_spans(spans: list[str],
                dict_hits: dict[str, EntityOut]) -> dict[str, str]:
    """B/C 共用语义链接核心: span 批量 embed → vec_entity ANN → 最高
    cosine ≥ ``_SEMANTIC_LINK_THRESHOLD`` → 链接既有 canonical 实体
    (in-place 并入 dict_hits), 返回 ``{span_lower: canonical}``。

    跨语言同义投影 (「护理担保」→ "aged care guarantee", 实测 0.53–0.81;
    无关对照 0.31–0.39 被阈值拦)。embedding 离线 ([]) / 索引不可用 →
    静默跳过 (降级不 crash, 循 resolver 红线; 主径不受影响)。
    """
    semantic_map: dict[str, str] = {}
    if not spans:
        return semantic_map
    try:
        import embedding
        vecs = embedding.embed_batch(spans)
    except Exception:
        return semantic_map
    import vec_index
    conn = db.get_conn()
    for span, vec in zip(spans, vecs):
        if not vec:
            continue
        try:
            top = vec_index.entity_topk(vec, 1)
        except Exception:
            return semantic_map
        if not top:
            continue
        eid, sim = top[0]
        if sim < _SEMANTIC_LINK_THRESHOLD:
            continue  # 无关注联 (< 0.45) 不误链
        row = conn.execute(
            "SELECT name, entity_type FROM entity WHERE id = ?",
            (eid,)).fetchone()
        if row is None:
            continue
        canonical = row["name"]
        semantic_map[span.lower()] = canonical
        if canonical in dict_hits:
            dict_hits[canonical].aliases.append(span)  # 语义 surface 参考归一
        else:
            dict_hits[canonical] = EntityOut(
                name=canonical, type=row["entity_type"], aliases=[span])
    return semantic_map


def semantic_fallback_hits(text: str) -> list[EntityOut]:
    """追加任务 C: 空提取段的零 LLM 向量兜底 — CJK run span 批量 embed →
    vec_entity ANN ≥0.45 → 链接既有实体。

    产出形态: **实体声明** (EntityOut, declared 类型) — 不硬造谓词边 (span
    无句式证据, 造边=臆测; 谓词留 wings)。C 不吞 A: 命中若干实体后段仍无
    edges → 调用方 (autodream) 仍按 A 层入队 (实体链接了, 语义内容还没提)。
    与 B 共用 :func:`_link_spans` 管道 (span 生成同 _CJK_RUN, 阈值同
    ``_SEMANTIC_LINK_THRESHOLD``), 触发位在空提取段兜底处。
    """
    masked = _mask_code_zones(text)  # 块分流 A 前置同防 (代码区 span 不产)
    spans: list[str] = []
    for m in _CJK_RUN.finditer(masked):
        s = m.group(0)
        if s not in spans:
            spans.append(s)
        if len(spans) >= _MAX_SEMANTIC_SPANS:
            break
    if not spans:
        return []
    hits: dict[str, EntityOut] = {}
    _link_spans(spans, hits)
    return list(hits.values())


# ── ②③ extractor.py 休眠模式复活 (§7 冻结指模式资产; 复活调用不违冻结) ──

def extract(text: str) -> Extraction:
    """Run the three-route placeholder extractor over ``text`` (M7).

    Wings-isomorphic output (``Extraction``): dictionary-linked entities +
    regex entities + relation edges, ``extractor_label='regex'`` (→ M6 落库
    extractor='regex', lif_source=0.4 档自动)。零 LLM / 零网络 — provider
    断供不影响本通道 (M6: 「provider 断=写入中断」缺陷解除的占位半边)。
    """
    masked = _mask_code_zones(text)
    gaz = _load_gazetteer()

    # ① 词典: 命中链到既有 canonical 名 (resolver step1 落到既有 id)。
    entities: dict[str, EntityOut] = _dict_entity_hits(masked, gaz)
    # ①+ 语义优先 (追加任务 B): 词典未覆盖的 CJK span 批量 embed → ANN →
    # ≥0.45 链接 canonical (跨语言同义投影); 返回 span→canonical 映射贯通
    # ②③ 路 (防关系路把已语义链接的 span 原文再声明为实体)。
    semantic_map = _semantic_entity_hits(masked, gaz, entities)
    dict_hit_names = {e.name for e in entities.values()}
    # 词典全域 surface (lower): regex 实体名 case-fold 命中任一已知 surface →
    # canonical 已覆盖, 不重复声明 (词典优先于 regex surface 形)。
    dict_surfaces_lower: set[str] = set(gaz.keys())

    # ② regex 实体模式 (休眠复活)。
    for ent in extractor._extract_entities(masked):
        name = ent["name"]
        if _ENV_NAME.match(name):
            continue  # env var 形态禁入 (adapter._ENV_PATTERN 精神)
        # 词典 canonical 已覆盖同一 surface (case-fold) → 不重复声明。
        if name.strip().lower() in dict_surfaces_lower or name in dict_hit_names:
            continue
        # 语义路已链接该 surface → canonical 已声明, 不重复。
        if semantic_map.get(name.strip().lower()):
            continue
        entities.setdefault(name, EntityOut(name=name, type=ent["entity_type"]))

    # ③ 关系模式 → edges; subject/object 必须 declared (wings 契约)。端点若
    # case-fold 命中词典 surface → 归一为 canonical (词典链接在提取面即生效,
    # 防 surface 形重复声明); 语义链接的 span 同归一到 canonical。
    def _canon(name: str) -> str:
        low = name.strip().lower()
        hit = gaz.get(low)
        if hit:
            return hit[0]
        sem = semantic_map.get(low)
        return sem if sem else name

    raw_facts = extractor._extract_facts(masked)
    # 端点级语义链接 (B 补): CJK run 与关系模式切出的 span 不一致 (run 含
    # 关系动词整串, 模式切短 span) — 对仍为原文的端点批 embed+ANN, ≥阈值归
    # 一 canonical, 防 span 形态差导致重复新建。
    endpoint_canon: dict[str, str] = {}
    pending_eps: list[str] = []
    for fact in raw_facts:
        for ep in (fact["subject"], fact["object"]):
            ep = (ep or "").strip()
            low = ep.lower()
            if (not ep or low in endpoint_canon or ep in pending_eps
                    or gaz.get(low) or semantic_map.get(low)):
                continue
            pending_eps.append(ep)
    if pending_eps:
        try:
            import embedding
            ep_vecs = embedding.embed_batch(pending_eps)
        except Exception:
            ep_vecs = []
        if ep_vecs:
            import vec_index
            econn = db.get_conn()
            for ep, vec in zip(pending_eps, ep_vecs):
                if not vec:
                    continue
                try:
                    top = vec_index.entity_topk(vec, 1)
                except Exception:
                    break
                if top and top[0][1] >= _SEMANTIC_LINK_THRESHOLD:
                    row = econn.execute(
                        "SELECT name FROM entity WHERE id = ?",
                        (top[0][0],)).fetchone()
                    if row is not None:
                        endpoint_canon[ep.lower()] = row["name"]

    def _canon_full(name: str) -> str:
        base = _canon(name)
        if base != name:
            return base
        return endpoint_canon.get(name.strip().lower(), name)

    edges: list[EdgeOut] = []
    for fact in raw_facts:
        subject, predicate, obj = (
            _canon_full(fact["subject"]), fact["predicate"],
            _canon_full(fact["object"]))
        if _ENV_NAME.match(subject) or _ENV_NAME.match(obj):
            continue
        for name, etype in ((subject, "concept"), (obj, "concept")):
            if name not in entities:
                entities[name] = EntityOut(name=name, type=etype)
        edges.append(EdgeOut(subject=subject, predicate=predicate, object=obj,
                             topic=""))

    return Extraction(
        entities=list(entities.values()),
        edges=edges,
        confidence=_PLACEHOLDER_CONFIDENCE if edges else 0.0,
        source_meta={
            "extractor_label": "regex",  # M6: SOURCE_WEIGHT['regex']=0.4 档
            "mode": "gazetteer",
            "dictionary_entities": len(dict_hit_names),
            "regex_edges": len(edges),
        },
    )
