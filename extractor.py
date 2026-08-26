"""mem-service extractor — regex EntityExtractor (ADR-5).

Borrows AO2 ``knowledge_graph.py:96-165`` regex patterns verbatim (review-
hardened: CJK character classes tightened to stop greedy散文 capture and
cross-sentence bleed). No LLM, deterministic.

Returns plain dicts ``{"entities": [...], "facts": [...]}`` keyed by name so
the cli layer can drive ``store`` directly. Fact objects are NOT dataclasses —
the store reifies them; we only carry (subject, predicate, obj, extractor).

Coverage ceiling (ADR-5 Consequences): English/technical/CJK-mix only; **pure
Chinese bare sentences hit zero** — semantic facts deferred to the LLM layer.
"""

from __future__ import annotations

import re
from typing import Any

# ── Entity patterns (9 classes, ADR-5 / AO2 :96-165) ────────────────

_CAPITALIZED_PHRASE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
_QUOTED_STRING = re.compile(r'"([^"]{2,80})"')
_TECHNICAL_TERM = re.compile(r"\b([A-Z][a-zA-Z0-9]*(?:\.[A-Z][a-zA-Z0-9]*)+)\b")
_CAMEL_CASE = re.compile(r"\b([a-z]+(?:[A-Z][a-z]+)+)\b")
_PASCAL_TECH = re.compile(r"\b([A-Z][a-z]+[A-Z][A-Za-z0-9]*)\b")
_ALLCAPS_PASCAL = re.compile(r"\b([A-Z]{2,}[a-z][A-Za-z0-9]*)\b")
_SNAKE_CASE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")
# CJK 书名号/方括号引号术语:《Logseq》、「记忆系统」、『知识图谱』。
_CJK_QUOTED = re.compile(r"[《「『]([^》」』]{2,20})[》」』]")
# CJK 中英混排:中文锚定的英文标识符(零宽锚定,不吞中文)。
_CJK_LATIN_MIX = re.compile(
    r"(?:(?<=[一-龥])[ \t]*[A-Za-z][A-Za-z0-9._-]{1,15}"
    r"|[A-Za-z][A-Za-z0-9._-]{1,15}(?=[ \t]*[一-龥]))"
)

# Pattern → (entity_type, min length). Order = AO2 precedence (later wins dedup).
_ENTITY_PATTERNS: list[tuple[re.Pattern, str, int]] = [
    (_CAPITALIZED_PHRASE, "named_entity", 2),
    (_QUOTED_STRING, "quoted_term", 3),
    (_TECHNICAL_TERM, "technical_term", 2),
    (_CAMEL_CASE, "identifier", 5),
    (_PASCAL_TECH, "technical_term", 4),
    (_ALLCAPS_PASCAL, "technical_term", 4),
    (_SNAKE_CASE, "identifier", 5),
    (_CJK_QUOTED, "quoted_term", 2),
    (_CJK_LATIN_MIX, "technical_term", 3),
]

# ── Relation patterns: 7 English predicates + CJK synonym sets (ADR-5) ──

_RELATION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(\w[\w\s]{1,40}?)\s+(?:is\s+an?\s+|are\s+)(.+?)(?:\.|,|$)", re.I), "is_a"),
    (re.compile(r"(\w[\w\s]{1,40}?)\s+(?:uses?|utilizes?)\s+(.+?)(?:\.|,|$)", re.I), "uses"),
    (re.compile(r"(\w[\w\s]{1,40}?)\s+(?:depends?\s+on|requires?)\s+(.+?)(?:\.|,|$)", re.I), "depends_on"),
    (re.compile(r"(\w[\w\s]{1,40}?)\s+(?:contains?|has?)\s+(.+?)(?:\.|,|$)", re.I), "contains"),
    (re.compile(r"(\w[\w\s]{1,40}?)\s+(?:belongs?\s+to)\s+(.+?)(?:\.|,|$)", re.I), "belongs_to"),
    (re.compile(r"(\w[\w\s]{1,40}?)\s+(?:implements?)\s+(.+?)(?:\.|,|$)", re.I), "implements"),
    (re.compile(r"(\w[\w\s]{1,40}?)\s+(?:connects?\s+to|links?\s+to)\s+(.+?)(?:\.|,|$)", re.I), "connected_to"),
    # v21 双语对称扩充 (向量验证 0.851/0.822, 用户指令: 中英覆盖语义范围一致)
    (re.compile(r"(\w[\w\s]{1,40}?)\s+(?:is\s+located\s+in|is\s+based\s+in)\s+(.+?)(?:\.|,|$)", re.I), "located_in"),
    (re.compile(r"(\w[\w\s]{1,40}?)\s+(?:causes?|leads?\s+to|results?\s+in)\s+(.+?)(?:\.|,|$)", re.I), "causes"),
    # v21b 高泛化原语集定版 (语料实测驱动: based_on 32次/prefers 26次/decided 20次 > implements 7次)
    (re.compile(r"(\w[\w\s]{1,40}?)\s+(?:is\s+based\s+on|derives?\s+from|builds?\s+on)\s+(.+?)(?:\.|,|$)", re.I), "based_on"),
    (re.compile(r"(\w[\w\s]{1,40}?)\s+(?:prefers?|likes?|favors?)\s+(.+?)(?:\.|,|$)", re.I), "prefers"),
    (re.compile(r"(\w[\w\s]{1,40}?)\s+(?:decided\s+to\s+(?:adopt|use|choose)|chose)\s+(.+?)(?:\.|,|$)", re.I), "decided"),
]

# CJK synonym sets: subject/obj char class [一-龥A-Za-z0-9] stops at space/
# punctuation (防 obj 贪心吞散文 + 跨句粘连). subject ≤8, obj ≤10.
# v21 双语对称补齐 (向量验证 belongs_to 0.892>is_a 0.806 → 「属于」拆出;
# implements 0.853 / connected_to 0.851 / located_in 0.851 / causes 0.822):
# EN 7 门 → CJK 侧补齐 belongs_to/implements/connected_to 三缺门,
# located_in/causes 两门双侧同步新增 — 中英谓词覆盖语义范围一致。
_CJK_RELATION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"([一-龥A-Za-z][一-龥A-Za-z0-9]{0,7})\s*是(?:一个|一种|一款)?\s*([一-龥A-Za-z][一-龥A-Za-z0-9]{0,9})"), "is_a"),
    (re.compile(r"([一-龥A-Za-z][一-龥A-Za-z0-9]{0,7})\s*(?:使用|采用|调用|利用)(?:了|着|过)?\s*([一-龥A-Za-z][一-龥A-Za-z0-9]{0,9})"), "uses"),
    (re.compile(r"([一-龥A-Za-z][一-龥A-Za-z0-9]{0,7})\s*(?:依赖|需要)(?:了|着|过)?\s*([一-龥A-Za-z][一-龥A-Za-z0-9]{0,9})"), "depends_on"),
    (re.compile(r"([一-龥A-Za-z][一-龥A-Za-z0-9]{0,7})\s*(?:包含|包括)(?:了|着|过)?\s*([一-龥A-Za-z][一-龥A-Za-z0-9]{0,9})"), "contains"),
    (re.compile(r"([一-龥A-Za-z][一-龥A-Za-z0-9]{0,7})\s*(?:属于|隶属于?)\s*([一-龥A-Za-z][一-龥A-Za-z0-9]{0,9})"), "belongs_to"),
    (re.compile(r"([一-龥A-Za-z][一-龥A-Za-z0-9]{0,7})\s*(?:实现|落地)(?:了|着|过)?\s*([一-龥A-Za-z][一-龥A-Za-z0-9]{0,9})"), "implements"),
    (re.compile(r"([一-龥A-Za-z][一-龥A-Za-z0-9]{0,7})\s*(?:连接到|对接|接入|集成(?:了)?)(?:了|着|过)?\s*([一-龥A-Za-z][一-龥A-Za-z0-9]{0,9})"), "connected_to"),
    (re.compile(r"([一-龥A-Za-z][一-龥A-Za-z0-9]{0,7})\s*(?:位于|坐落于?)\s*([一-龥A-Za-z][一-龥A-Za-z0-9]{0,9})"), "located_in"),
    (re.compile(r"([一-龥A-Za-z][一-龥A-Za-z0-9]{0,7})\s*(?:导致|引发)(?:了|着|过)?\s*([一-龥A-Za-z][一-龥A-Za-z0-9]{0,9})"), "causes"),
    # v21b 原语集定版: 「基于」从 uses 拆出 (0.777 vs 0.768 向量难分, 模式硬分——兜底分工实证);
    # prefers/decided 为记忆语料特有高频 (通用 IE 关系集无此门, 语料 26/20 次实据)
    (re.compile(r"([一-龥A-Za-z][一-龥A-Za-z0-9]{0,7})\s*(?:基于|参考了?|借鉴)(?:了|着|过)?\s*([一-龥A-Za-z][一-龥A-Za-z0-9]{0,9})"), "based_on"),
    (re.compile(r"([一-龥A-Za-z][一-龥A-Za-z0-9]{0,7})\s*(?:喜欢|偏好|偏爱|首选)(?:了|着|过)?\s*([一-龥A-Za-z][一-龥A-Za-z0-9]{0,9})"), "prefers"),
    (re.compile(r"([一-龥A-Za-z][一-龥A-Za-z0-9]{0,7})\s*(?:决定(?:采用|使用|选)|选定|选择了|拍板)(?:了|着|过)?\s*([一-龥A-Za-z][一-龥A-Za-z0-9]{0,9})"), "decided"),
]

MAX_ENTITIES = 20
MAX_FACTS = 15


def extract(text: str) -> dict[str, list[dict[str, Any]]]:
    """Run the regex EntityExtractor over ``text``.

    Returns ``{"entities": [...], "facts": [...]}``:

    - entities: ``{"name", "entity_type"}`` deduped by name.
    - facts: ``{"subject", "predicate", "object"}`` — names, not ids; the
      cli resolves them to entity ids via ``store``. ``extractor`` field is
      stamped by the cli (always "regex" per ADR-5), not here.
    """
    return {"entities": _extract_entities(text), "facts": _extract_facts(text)}


def _extract_entities(text: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for pattern, etype, min_len in _ENTITY_PATTERNS:
        for m in pattern.finditer(text):
            name = (m.group(1) if m.groups() else m.group(0)).strip()
            if len(name) >= min_len and name not in seen:
                seen.add(name)
                out.append({"name": name, "entity_type": etype})
            if len(out) >= MAX_ENTITIES:
                return out
    return out


def _extract_facts(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for patterns in (_RELATION_PATTERNS, _CJK_RELATION_PATTERNS):
        for pattern, predicate in patterns:
            for m in pattern.finditer(text):
                subject = m.group(1).strip()
                obj = m.group(2).strip()
                if len(subject) > 1 and len(obj) > 1:
                    out.append({"subject": subject, "predicate": predicate, "object": obj})
                if len(out) >= MAX_FACTS:
                    return out
    return out


# ── self-check ──────────────────────────────────────────────────────

def _demo() -> None:
    # Acceptance-shaped: pure CJK fact with Latin object via 使用.
    r = extract("用户使用 rust 进行开发")
    assert {"name": "rust", "entity_type": "technical_term"} in r["entities"], r
    assert {"subject": "用户", "predicate": "uses", "object": "rust"} in r["facts"], r
    # CJK is_a + quoted term.
    r2 = extract("Logseq 是笔记工具")
    assert {"subject": "Logseq", "predicate": "is_a", "object": "笔记工具"} in r2["facts"], r2
    # English predicate — AO2 uses-pattern captures obj up to . / , / EOL,
    # so the object run is the whole phrase (deterministic; spec-correct).
    r3 = extract("FastAPI uses Pydantic.")
    assert {"subject": "FastAPI", "predicate": "uses", "object": "Pydantic"} in r3["facts"], r3
    # v21 双语对称补齐: CJK 三缺门 + 双侧新增两门。
    r4 = extract("该模块实现了重试协议")
    assert {"subject": "该模块", "predicate": "implements", "object": "重试协议"} in r4["facts"], r4
    r5 = extract("该项目属于智谱实验室")
    assert {"subject": "该项目", "predicate": "belongs_to", "object": "智谱实验室"} in r5["facts"], r5
    r6 = extract("网关连接到计费服务")
    assert {"subject": "网关", "predicate": "connected_to", "object": "计费服务"} in r6["facts"], r6
    r7 = extract("悉尼歌剧院位于便利朗角")
    assert {"subject": "悉尼歌剧院", "predicate": "located_in", "object": "便利朗角"} in r7["facts"], r7
    r8 = extract("缓存失效导致线上故障")
    assert {"subject": "缓存失效", "predicate": "causes", "object": "线上故障"} in r8["facts"], r8
    # v21b 原语集: 拆出 based_on + 记忆语料特有 prefers/decided。
    r9 = extract("新方案基于向量索引")
    assert {"subject": "新方案", "predicate": "based_on", "object": "向量索引"} in r9["facts"], r9
    r10 = extract("用户偏好深色主题")
    assert {"subject": "用户", "predicate": "prefers", "object": "深色主题"} in r10["facts"], r10
    r11 = extract("团队决定采用智谱")
    assert {"subject": "团队", "predicate": "decided", "object": "采用智谱"} in r11["facts"], r11
    print("ok")


if __name__ == "__main__":
    _demo()
