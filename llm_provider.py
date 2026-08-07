"""mem-service llm_provider — LLMProvider Protocol + CCRProvider (ADR-5b).

The adapter (``adapter.py``) drives an arbitrary ``LLMProvider`` to extract
facts the regex layer (ADR-5) cannot reach — pure Chinese bare sentences,
synonyms, rewrites. ``extract_facts`` returns a normalized ``Extraction``
(facts list + confidence + source_meta); the adapter fans it out N-way,
votes, and falls back to regex when providers are absent or low-confidence.

Providers are *passive*: a provider that is unreachable, errors, or returns
garbage yields an empty facts list + low confidence (0.0), never raises. The
adapter decides whether to trust / fall back.

ZhipuAnthropicProvider 直连智谱 (open.bigmodel.cn/api/anthropic, glm-5-turbo).
CCR router proxy removed — provider 直连少一跳. claude-api / LMstudio
providers are stubs — concrete impl deferred until a deploy target exists.
Kept minimal: the Protocol is the seam, new providers slot in by implementing
``extract_facts``.
"""

from __future__ import annotations

import json
import os
import pathlib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# ── Normalized return shape (ADR-5b: facts[], confidence, source_meta) ───

@dataclass
class FactOut:
    """One fact surfaced by an LLM provider, in extractor.py's shape.

    Mirrors ``extractor._extract_facts`` output so the adapter/cli can route
    LLM facts and regex facts through the same store path: subject/predicate/
    object are *names*, the cli resolves them to entity ids.
    """
    subject: str
    predicate: str
    object: str


@dataclass
class Extraction:
    """Provider/adapter output: facts + an aggregate confidence ∈ [0,1] +
    opaque source metadata (provider name, model, raw response bits for
    debugging). Empty ``facts`` + confidence 0.0 = "nothing extracted"."""
    facts: list[FactOut] = field(default_factory=list)
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


# Fixed prompt: zero-shot fact extraction → strict JSON. One prompt, no
# variation: per-call variation (the "butterfly wing" diversity) lives in the
# adapter (prompt transforms / multi-provider), not here. ponytail: a single
# hardened prompt beats N hand-tuned variants at v3-stage-1 scale.
_EXTRACT_PROMPT = """从下面的文本抽取事实三元组,只返回 JSON。
格式: {"facts": [{"subject": "...", "predicate": "...", "object": "..."}]}
predicate 从这些里选: is_a, uses, depends_on, contains, belongs_to, implements, connected_to, part_of, relates_to
规则:
- 保留专有名词/技术术语/缩略词(如 a2a, mesh, A2A, ratatui, pydantic-ai)原样作为 subject/object,不要泛化成"系统/框架/agent"。
- 同义不同写法(a2a/A2A)视为同一实体,用原文形式。
- 找不到任何事实就返回 {"facts": []},不要解释。
示例:
文本: native agent 自成 A2A 节点, 形成内部 mesh
{"facts": [{"subject": "native agent", "predicate": "is_a", "object": "A2A node"}, {"subject": "native agent", "predicate": "part_of", "object": "A2A mesh"}]}
文本: """

# CCRProvider removed — ZhipuAnthropicProvider 直连 open.bigmodel.cn/api/anthropic,
# 不经 localhost:3456 ccr 路由 (provider 直连, 少一跳)。


# ── ZhipuAnthropicProvider — 智谱直连 Anthropic 协议(glm-5-turbo, coding plan)──

@dataclass
class ZhipuAnthropicProvider:
    """智谱 Anthropic 协议直连(不经 CCR localhost:3456 中转), model glm-5-turbo。

    base_url https://open.bigmodel.cn/api/anthropic, Anthropic Messages 格式
    (/v1/messages, x-api-key + anthropic-version)。api_key 从 env ZHIPU_API_KEY
    或 CCR config(~/.claude-code-router/config.json Providers zhipu-anthropic)读
    (key 不进 git)。比 CCRProvider 少一跳(直连智谱)。国内服务(open.bigmodel.cn)
    → ProxyHandler({}) 禁境外代理直连(host-network-proxy 教训)。Failures → empty conf 0.0。
    """
    base_url: str = "https://open.bigmodel.cn/api/anthropic"
    model: str = "glm-5-turbo"
    api_key: str = ""  # 空 → _load_zhipu_key 从 env/CCR config 读
    timeout: float = 60.0

    def extract_facts(self, text: str) -> Extraction:
        key = self.api_key or _load_zhipu_key()
        if not key:
            return Extraction(confidence=0.0, source_meta={
                "provider": "zhipu", "error": "no api_key (set ZHIPU_API_KEY or CCR config)"})
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
        facts = _parse_facts(content)
        conf = 0.7 if facts else 0.0
        return Extraction(facts=facts, confidence=conf,
                          source_meta={"provider": "zhipu", "model": self.model})


def _load_zhipu_key() -> str:
    """智谱 API key: env ZHIPU_API_KEY 优先, 否则 CCR config
    (~/.claude-code-router/config.json Providers zhipu-anthropic.api_key)。空 if 都无。"""
    key = os.environ.get("ZHIPU_API_KEY", "")
    if key:
        return key
    try:
        cfg = pathlib.Path.home() / ".claude-code-router" / "config.json"
        for prov in json.loads(cfg.read_text(encoding="utf-8")).get("Providers", []):
            if prov.get("name") == "zhipu-anthropic":
                return prov.get("api_key", "") or ""
    except (OSError, ValueError, KeyError):
        pass
    return ""


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


def _parse_facts(content: str) -> list[FactOut]:
    """Best-effort JSON extraction from the model's text response.

    Models occasionally wrap JSON in prose or fences; find the first {...}
    blob and parse that. Malformed → empty list (provider-level failure
    surfaces as confidence 0.0; adapter fallback handles it).
    """
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        doc = json.loads(content[start:end + 1])
    except json.JSONDecodeError:
        return []
    out: list[FactOut] = []
    for f in doc.get("facts", []):
        try:
            out.append(FactOut(
                subject=str(f["subject"]).strip(),
                predicate=str(f["predicate"]).strip(),
                object=str(f["object"]).strip(),
            ))
        except (KeyError, TypeError):
            continue
    return out


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


@dataclass
class LMStudioProvider:
    """Stub: local LMStudio OpenAI-compat route. Not wired until a local
    model deploy target exists. Returns empty extraction."""
    base_url: str = "http://localhost:1234"
    model: str = "local"

    def extract_facts(self, text: str) -> Extraction:
        return Extraction(confidence=0.0, source_meta={
            "provider": "lmstudio", "error": "stub, not implemented"})
