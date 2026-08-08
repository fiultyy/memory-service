"""mem-service llm_provider — LLMProvider Protocol + ZhipuAnthropicProvider (ADR-5b).

The adapter (``adapter.py``) drives an arbitrary ``LLMProvider`` to extract
facts the regex layer (ADR-5) cannot reach — pure Chinese bare sentences,
synonyms, rewrites. ``extract_facts`` returns a normalized ``Extraction``
(facts list + confidence + source_meta); the adapter fans it out N-way,
votes, and falls back to regex when providers are absent or low-confidence.

Providers are *passive*: a provider that is unreachable, errors, or returns
garbage yields an empty facts list + low confidence (0.0), never raises. The
adapter decides whether to trust / fall back.

ZhipuAnthropicProvider 直连智谱 (open.bigmodel.cn/api/anthropic, glm-5-turbo)。
base_url/model/api_key 全从 env 读 (cli._load_env 从 .env 加载, 自包含不依赖 CCR)。
claude-api / LMstudio providers are stubs — concrete impl deferred until a deploy target exists.
Kept minimal: the Protocol is the seam, new providers slot in by implementing
``extract_facts``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# ── Normalized return shape (graph-aware: entities[] + edges[], R1 档 1) ──
#
# Breaking change vs ADR-5b: ``FactOut`` deleted, ``Extraction.facts`` deleted.
# The old schema was *graph-blind* — object was a free string, never a declared
# entity reference → object_id 恒 NULL → zero entity↔entity edges (R1 §0).
# New shape (LangChain _Graph + Graphiti Edge + 封闭谓词融合): edges reference
# *declared* entity names; subject AND object both resolve to entities.

@dataclass
class EntityOut:
    """One declared entity. ``name`` is the verbatim surface form an edge
    references (case preserved). ``type`` from the closed enum; aliases are
    alternative spellings, persisted to entity.aliases (ADR-D7)."""
    name: str
    type: str = "concept"
    aliases: list[str] = field(default_factory=list)


@dataclass
class EdgeOut:
    """One edge between two *declared* entities. subject/object MUST be a
    name appearing in ``EntityOut.name`` (enforced by prompt + cli/autodream
    resolve both sides to entities). predicate from the closed 9-set.
    ``topic`` (ADR-C) is a one-sentence human-readable fact the LLM emits per
    edge — projection uses it as filename slug + index title + description."""
    subject: str
    predicate: str
    object: str
    topic: str = ""


@dataclass
class Extraction:
    """Provider/adapter output: declared entities + edges between them, an
    aggregate confidence ∈ [0,1], and opaque source metadata. Empty
    entities/edges + confidence 0.0 = "nothing extracted"."""
    entities: list[EntityOut] = field(default_factory=list)
    edges: list[EdgeOut] = field(default_factory=list)
    confidence: float = 0.0
    source_meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    """Extract facts from natural-language ``text``.

    Pure: no side effects, never raises on network/model errors (those map to
    an empty ``Extraction`` with confidence 0.0 and a note in source_meta).
    The adapter owns fan-out / voting / fallback; a provider only reports its
    own single-shot best guess.
    """

    def extract_facts(self, text: str) -> Extraction: ...

    def dedupe_entity(self, new_name: str, new_type: str,
                      candidates: list) -> dict: ...


# Fixed two-step prompt: declare entities first, then edges between declared
# entities (R1 §A3). object is ALWAYS another declared entity, never a free
# phrase → cli/autodream _ensure_entity both sides → object_id 必非空 →
# entity↔entity edges emerge (graph-blind schema fixed). ADR-C: each edge also
# carries a human-readable ``topic`` (one short sentence stating the fact) —
# projection uses it as filename slug + index title + description. One prompt;
# per-call butterfly-wing diversity lives in the adapter, not here. ponytail: a
# single hardened prompt beats N hand-tuned variants at v3-stage-1 scale.
_EXTRACT_PROMPT = """你是知识图谱抽取器。分两步,只返回 JSON。

第一步:从文本里抽出全部实体。每个实体:
- name: 专有名词/技术术语原样(保留 a2a/A2A/ratatui/pydantic-ai 原形),同一实体只声明一次,用最完整的写法
- type: 从 [component, protocol, tool, architecture, concept, org, person] 里选一个
- aliases: 该实体在文中出现过的其他写法(大小写/连字符/缩写),没有就空数组

第二步:在【已声明的实体】之间抽边。每条边:
- subject / object: 必须是上面 entities 里出现过的 name(原样,不可改写)
- predicate: 从 [is_a, uses, depends_on, contains, belongs_to, implements, connected_to, part_of, relates_to] 里选
- topic: 用一句简短自然语言概括这条事实(subject 与 object 之间通过 predicate 表达的关系),作为可读标题(<= 30 字, 无标点结尾, 无换行)

硬规则:
- object 永远是【另一个实体】,不是描述性短语。如果某关系的目标是描述(如"是一种去中心化协议"),把它拆成:先声明该描述为实体,再连边。
- edges 里每个 subject / object 必须 verbatim 出现在 entities[].name 里。
- topic 是给人读的一句话事实概括,不要复述三元组原文,要凝练。
- 找不到任何实体/边就返回 {"entities": [], "edges": []},不要解释。

输出格式:
{"entities": [{"name":"...","type":"...","aliases":[...]}],
 "edges":    [{"subject":"...","predicate":"...","object":"...","topic":"..."}]}

示例:
文本: native agent 自成 A2A 节点, 形成内部 mesh
{"entities": [{"name":"native agent","type":"component","aliases":[]},{"name":"A2A","type":"protocol","aliases":["a2a"]},{"name":"mesh","type":"architecture","aliases":[]}],
 "edges":    [{"subject":"native agent","predicate":"is_a","object":"A2A","topic":"native agent 是 A2A 协议节点"},{"subject":"native agent","predicate":"part_of","object":"mesh","topic":"native agent 组成内部 mesh"}]}
文本: """


# ── Entity dedupe prompt (ADR-D3 two-step merge, Graphiti dedupe_nodes style) ─
# Synonymy judge: same real-world referent (异写/缩写/译名) → merge; merely
# related or homonymous → never merge. Few-shot hardens the homonym trap.
_DEDUPE_PROMPT = """你是知识图谱实体去重裁判。判断"待判实体"是否与候选列表里的某个实体是【同一实体的不同写法】(同义异写)。
只有真正同义(指代真实世界同一对象)才算重复; 仅"相关"或"同名不同义"绝不算重复。

规则:
- 同义异写 → 合: 指向真实世界同一对象的不同名称/缩写/大小写/译名。
- 相关但不同义 → 不合: 同名/近名但指向不同对象(同名异物)。
- 不确定 → 不合(宁可新建, 不误合)。

候选字段: id(实体的内部 id), name(实体名), type(类型), score(向量余弦相似度, 越高越像)。
返回 JSON, 只含 duplicate_id:
- 命中候选: {{"duplicate_id": "<候选 id>"}}
- 不命中: {{"duplicate_id": null}}

示例:
待判: name="New York City" type="concept"
候选: [{{"id":"e1","name":"NYC","type":"concept","score":0.92}}]
{{"duplicate_id": "e1"}}  ← NYC 是 New York City 的缩写, 同义异写, 合

待判: name="Java" type="location"
候选: [{{"id":"e2","name":"Java编程语言","type":"tool","score":0.86}}]
{{"duplicate_id": null}}  ← Java(印尼爪哇岛)≠ Java 编程语言, 同名不同义, 不合

待判: name="Java编程语言" type="tool"
候选: [{{"id":"e3","name":"Java","type":"tool","score":0.88}}]
{{"duplicate_id": "e3"}}  ← 都指 Java 编程语言, 同义, 合

待判: name="Python" type="tool"
候选: [{{"id":"e4","name":"Python蟒蛇","type":"concept","score":0.81}}]
{{"duplicate_id": null}}  ← Python 语言 ≠ 蟒蛇, 同名不同义, 不合

待判: name="中国" type="location"
候选: [{{"id":"e5","name":"中华人民共和国","type":"location","score":0.85}}]
{{"duplicate_id": "e5"}}  ← 中国 是中华人民共和国的简称, 同义, 合

现在判断:
待判: name="{new_name}" type="{new_type}"
候选: {candidates}
"""

# ── ZhipuAnthropicProvider — 智谱直连 Anthropic 协议(glm-5-turbo) ────────

@dataclass
class ZhipuAnthropicProvider:
    """智谱 Anthropic 协议直连, model glm-5-turbo。

    base_url/model 从 env (MEM_LLM_BASE_URL/MEM_LLM_MODEL) 读, 默认智谱
    (open.bigmodel.cn/api/anthropic, Anthropic Messages /v1/messages,
    x-api-key + anthropic-version)。api_key 从 env ZHIPU_API_KEY 读
    (cli._load_env 从同目录 .env 加载, key 不进 git)。国内服务(open.bigmodel.cn)
    → ProxyHandler({}) 禁境外代理直连(host-network-proxy 教训)。Failures → empty conf 0.0。
    """
    base_url: str = "https://open.bigmodel.cn/api/anthropic"
    model: str = "glm-5-turbo"
    api_key: str = ""  # 空 → _load_zhipu_key 从 env ZHIPU_API_KEY 读
    timeout: float = 60.0

    def extract_facts(self, text: str) -> Extraction:
        key = self.api_key or _load_zhipu_key()
        if not key:
            return Extraction(confidence=0.0, source_meta={
                "provider": "zhipu", "error": "no api_key (set ZHIPU_API_KEY in .env)"})
        body = json.dumps({
            "model": self.model, "max_tokens": 512,
            "messages": [{"role": "user", "content": _EXTRACT_PROMPT + text}],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/messages", data=body,
            headers={"Content-Type": "application/json",
                     "x-api-key": key, "anthropic-version": "2023-06-01"},
            method="POST")
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            return Extraction(confidence=0.0, source_meta={
                "provider": "zhipu", "error": f"network: {e!r}"})
        content = _extract_text(raw)
        if content is None:
            return Extraction(confidence=0.0, source_meta={
                "provider": "zhipu", "model": self.model,
                "error": "no content block", "raw": raw[:200]})
        entities, edges = _parse_facts(content)
        conf = 0.7 if edges else 0.0
        return Extraction(entities=entities, edges=edges, confidence=conf,
                          source_meta={"provider": "zhipu", "model": self.model})

    def dedupe_entity(self, new_name: str, new_type: str,
                      candidates: list) -> dict:
        """Decide whether ``new_name`` duplicates an existing entity (ADR-D3).

        Graphiti dedupe_nodes style: few-shot LLM judges synonymy vs mere
        relatedness/homonymy. Returns ``{"duplicate_id": str | None}``.
        Offline / no key / network error / parse failure → ``{"duplicate_id": None}``
        (降级为新建, 不 crash)。
        """
        key = self.api_key or _load_zhipu_key()
        if not key:
            return {"duplicate_id": None}
        body = json.dumps({
            "model": self.model, "max_tokens": 256,
            "messages": [{"role": "user", "content": _DEDUPE_PROMPT.format(
                new_name=new_name, new_type=new_type,
                candidates=json.dumps(candidates, ensure_ascii=False))}],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/messages", data=body,
            headers={"Content-Type": "application/json",
                     "x-api-key": key, "anthropic-version": "2023-06-01"},
            method="POST")
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return {"duplicate_id": None}
        content = _extract_text(raw)
        if content is None:
            return {"duplicate_id": None}
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end > start:
            try:
                doc = json.loads(content[start:end + 1])
                did = doc.get("duplicate_id")
                if did is None or isinstance(did, str):
                    return {"duplicate_id": did}
            except json.JSONDecodeError:
                pass
        return {"duplicate_id": None}


def _load_zhipu_key() -> str:
    """智谱 API key: 仅 env ZHIPU_API_KEY (cli._load_env 从同目录 .env 加载)。
    CCR config fallback 已移除 — mem-service 与 CCR 解耦, 自包含。"""
    return os.environ.get("ZHIPU_API_KEY", "")


def _extract_text(raw: str) -> str | None:
    """Pull the assistant text out of an Anthropic Messages response."""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return None
    for block in doc.get("content", []):
        if block.get("type") == "text":
            return block.get("text", "")
    return None


def _parse_facts(content: str) -> tuple[list[EntityOut], list[EdgeOut]]:
    """Best-effort JSON extraction from the model's text response.

    Models occasionally wrap JSON in prose or fences; find the first {...}
    blob and parse that. Returns (entities, edges). Malformed → ([], [])
    (provider-level failure surfaces as confidence 0.0; adapter handles it).

    Hard rule enforced here: an edge whose subject/object is NOT verbatim in
    entities[].name is dropped (R1 §A2 hard constraint — keeps the graph
    well-formed; matches Graphiti/LangChain rejection of dangling refs).
    """
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return [], []
    try:
        doc = json.loads(content[start:end + 1])
    except json.JSONDecodeError:
        return [], []

    entities: list[EntityOut] = []
    for e in doc.get("entities", []):
        try:
            name = str(e["name"]).strip()
            if not name:
                continue
            etype = str(e.get("type") or "concept").strip() or "concept"
            raw_aliases = e.get("aliases") or []
            aliases = [str(a).strip() for a in raw_aliases
                       if isinstance(a, (str, int, float)) and str(a).strip()]
            entities.append(EntityOut(name=name, type=etype, aliases=aliases))
        except (KeyError, TypeError):
            continue

    declared = {ent.name for ent in entities}
    edges: list[EdgeOut] = []
    for f in doc.get("edges", []):
        try:
            subj = str(f["subject"]).strip()
            obj = str(f["object"]).strip()
            pred = str(f["predicate"]).strip()
            if not subj or not obj or not pred:
                continue
            # R1 §A2: edge endpoints must be declared entity names. Drop
            # dangling refs (LLM 遵守 prompt 但仍可能漏 → 不污染 graph).
            if subj not in declared or obj not in declared:
                continue
            # ADR-C: topic — best-effort read (LLM 偶尔漏 → topic=""), strip
            # newlines so a multiline topic can't break filename/index line.
            raw_topic = f.get("topic")
            topic = str(raw_topic).strip() if raw_topic is not None else ""
            topic = topic.replace("\r", " ").replace("\n", " ").strip()
            edges.append(EdgeOut(subject=subj, predicate=pred, object=obj,
                                 topic=topic))
        except (KeyError, TypeError):
            continue
    return entities, edges


# ── Stub providers (ADR-5b "备", concrete impl deferred to a deploy target) ─

@dataclass
class ClaudeAPIProvider:
    """Stub: Anthropic claude-api route. Not wired until a deploy target
    wants a non-ccr path. Returns empty extraction (adapter falls back)."""
    api_key: str = ""
    model: str = "claude-sonnet"

    def extract_facts(self, text: str) -> Extraction:
        return Extraction(confidence=0.0, source_meta={
            "provider": "claude-api", "error": "stub, not implemented"})

    def dedupe_entity(self, new_name: str, new_type: str,
                      candidates: list) -> dict:
        raise NotImplementedError("stub, not implemented")


@dataclass
class LMStudioProvider:
    """Stub: local LMStudio OpenAI-compat route. Not wired until a local
    model deploy target exists. Returns empty extraction."""
    base_url: str = "http://localhost:1234"
    model: str = "local"

    def extract_facts(self, text: str) -> Extraction:
        return Extraction(confidence=0.0, source_meta={
            "provider": "lmstudio", "error": "stub, not implemented"})

    def dedupe_entity(self, new_name: str, new_type: str,
                      candidates: list) -> dict:
        raise NotImplementedError("stub, not implemented")
