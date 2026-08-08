"""mem-service adapter — butterfly-wing LLM extraction (ADR-5b).

Ingest seam: N-way fan-out over LLM providers → majority vote → confidence.
**No regex fallback** (ADR-5 fallback removed by directive): if no provider is
reachable, or every wing errors out with no facts, ``extract_facts`` **raises**
— the caller sees LLM is down rather than silently ingesting low-quality regex
facts. An empty vote *without* errors is legitimate (the LLM judged the text
holds no fact) and is returned as-is.

API: ``adapter.extract_facts(text, providers=None)`` → ``llm_provider.Extraction``.
``providers=None`` ⇒ ``default_providers()`` = [ZhipuAnthropicProvider] 直连智谱.
"""

from __future__ import annotations

import os
import re

from llm_provider import (
    EdgeOut,
    EntityOut,
    Extraction,
    LLMProvider,
    ZhipuAnthropicProvider,
)

# Butterfly-wing fan-out. N=3 per ADR-5b: the same provider under 3 prompt
# transforms (diversity via prompt variation).
DEFAULT_WINGS = 3

# Quality guard: entities must be conceptual (projects, tech, roles, orgs),
# never file paths, function signatures, code symbols, or env var names.
_ENTITY_QUALITY = (
    "实体必须是概念级名称（项目名/技术名/角色名/组织名），"
    "禁止使用文件路径、函数签名、代码符号或环境变量名作为实体。"
    "错误示例：data/memory.db、adapter.py、ZhipuAnthropicProvider._load_zhipu_key()、ZHIPU_API_KEY。"
    "正确示例：mem-service、CC memory、KG、智谱、Claude Code。"
)

# Prompt transforms for butterfly-wing diversity. Each wraps the input text in
# a different framing; the JSON contract is identical, only the surface varies.
_WING_PROMPTS = [
    f"抽取事实: {_ENTITY_QUALITY} ",                        # bare
    f"请仔细阅读并提取其中的事实三元组: {_ENTITY_QUALITY} ",  # careful reframe
    f"识别以下文本中的实体关系: {_ENTITY_QUALITY} ",         # relation-focused reframe
]


def extract_facts(
    text: str,
    providers: list[LLMProvider] | None = None,
    wings: int = DEFAULT_WINGS,
) -> Extraction:
    """Extract facts from ``text`` via butterfly-wing LLM voting.

    - ``providers=None`` ⇒ ``default_providers()`` (ZhipuAnthropicProvider 直连).
    - **No reachable provider ⇒ ``RuntimeError``** (block; regex fallback removed).
    - Otherwise: fan out ``wings`` calls across the providers (round-robin when
      len(providers) < wings), vote on identical (subject, predicate, object)
      edge triples (quorum ⌈n/2⌉), union entities across wings, aggregate
      confidence as the max.
    - Empty vote **with provider errors** ⇒ ``RuntimeError`` (LLM unavailable:
      no key / network / parse). Empty vote **without errors** ⇒ returned
      as-is (the LLM legitimately found no edge).
    """
    if providers is None:
        providers = default_providers()
    active = [p for p in providers if _is_reachable(p)]
    if not active:
        raise RuntimeError(
            "no reachable LLM provider — regex fallback removed; "
            "set ZHIPU_API_KEY (or CCR config zhipu-anthropic) to unblock")

    # 蝴蝶翼并行 fan-out (N wing 并发, 非 serial — N× 加速, 单 wing 超时不拖累其他)
    from concurrent.futures import ThreadPoolExecutor

    def _wing(i: int) -> Extraction:
        provider = active[i % len(active)]
        framed = f"{_WING_PROMPTS[i % len(_WING_PROMPTS)]}\n{text}"
        try:
            return provider.extract_facts(framed)
        except Exception as e:  # Protocol forbids raising, but defend anyway
            return Extraction(
                confidence=0.0,
                source_meta={"provider": type(provider).__name__, "error": repr(e)})

    with ThreadPoolExecutor(max_workers=wings) as ex:
        extractions = list(ex.map(_wing, range(wings)))

    voted = _vote(extractions)
    if not voted.edges:
        errs = [e.source_meta.get("error") for e in extractions
                if e.source_meta.get("error")]
        if errs:
            raise RuntimeError(
                f"LLM providers returned no edges (errors: {errs[:2]}). "
                "regex fallback removed — block instead of silent low-quality ingest.")
    return voted


# ── voting / aggregation ──────────────────────────────────────────────

# #2: env 变量名正则 (全大写,至少一个下划线,如 CLAUDE_CODE_SESSION_ID/ZHIPU_API_KEY)
_ENV_PATTERN = re.compile(r'^[A-Z][A-Z0-9_]*_[A-Z0-9_]+$')


def _is_env_entity(name: str) -> bool:
    """检查 name 是否匹配 env 变量名 pattern (全大写+下划线)。"""
    return bool(_ENV_PATTERN.match(name.strip()))


def _vote(extractions: list[Extraction]) -> Extraction:
    """Vote edges by quorum majority; union entities across wings; confidence = max.

    - **edges**: per (subject, predicate, object) case-fold key, a triple
      survives if ≥ ⌈n/2⌉ wings produced it. Surviving edge keeps the first
      wing's surface form (case preserved). Same logic as the old fact vote,
      now on ext.edges.
    - **entities**: cross-wing union, dedupe by case-fold name (keep first
      surface form, merge aliases). R1 does not require entity voting —
      union+dedupe suffices.
    - env-pattern filter (_is_env_entity) applies to entity.name AND edge
      subject/object.

    ponytail: 最浅归一 (case-fold only) — no lemmatize/alias store (Tier 2).
    """
    n = len(extractions)
    quorum = (n + 1) // 2  # ⌈n/2⌉: 3→2, 2→1, 1→1

    # ── edges: quorum vote ──
    triple_wings: dict[tuple[str, str, str], list[tuple[int, EdgeOut]]] = {}
    for wi, ext in enumerate(extractions):
        for e in ext.edges:
            key = (e.subject.strip().lower(), e.predicate.strip().lower(),
                   e.object.strip().lower())
            triple_wings.setdefault(key, []).append((wi, e))

    surviving: list[EdgeOut] = []
    contributing_confidences: list[float] = []
    agree_hist: list[int] = []
    for key, wing_edges in triple_wings.items():
        agree_hist.append(len(wing_edges))
        if len(wing_edges) >= quorum:
            edge = wing_edges[0][1]  # first surface form (case preserved)
            # env-pattern filter: skip if subject or object is an env var name
            if _is_env_entity(edge.subject) or _is_env_entity(edge.object):
                continue
            surviving.append(edge)
            for wi, _ in wing_edges:
                contributing_confidences.append(extractions[wi].confidence)

    # ── entities: union + dedupe by case-fold name ──
    seen_cf: dict[str, EntityOut] = {}
    for ext in extractions:
        for ent in ext.entities:
            if _is_env_entity(ent.name):
                continue
            cf = ent.name.strip().lower()
            if not cf:
                continue
            if cf in seen_cf:
                # merge aliases (keep first surface form)
                merged = seen_cf[cf]
                for a in ent.aliases:
                    if a and a not in merged.aliases:
                        merged.aliases.append(a)
            else:
                seen_cf[cf] = EntityOut(
                    name=ent.name, type=ent.type, aliases=list(ent.aliases))

    conf = max(contributing_confidences) if contributing_confidences else 0.0
    return Extraction(
        entities=list(seen_cf.values()), edges=surviving, confidence=conf,
        source_meta={
            "wings": n, "quorum": quorum,
            "agreement": sorted(agree_hist, reverse=True),
            "mode": "majority",
        },
    )


# ── provider reachability (cheap pre-check, no full call) ─────────────

def _is_reachable(provider: LLMProvider) -> bool:
    """True if ``provider`` looks usable. Cheap TCP probe (base_url) or a
    stub-excluding ``extract_facts("")`` probe (no base_url). No token spend."""
    base_url = getattr(provider, "base_url", None)
    if base_url:
        return _tcp_reachable(base_url, timeout=2.0)
    try:
        probe = provider.extract_facts("")
    except Exception:
        return False
    err = str(probe.source_meta.get("error", ""))
    if "stub" in err or "not implemented" in err:
        return False
    return True


def _tcp_reachable(base_url: str, timeout: float = 2.0) -> bool:
    """TCP-connect the host:port of ``base_url``. Cheap reachability probe —
    no HTTP request, no model, no token spend. True if the socket opens."""
    import urllib.parse
    try:
        parsed = urllib.parse.urlparse(base_url)
    except ValueError:
        return False
    host = parsed.hostname
    if not host:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── default providers ─────────────────────────────────────────────────

def default_providers() -> list[LLMProvider]:
    """LLM provider list(蝴蝶翼抽取): ZhipuAnthropicProvider 直连智谱
    (glm-5-turbo, open.bigmodel.cn/api/anthropic)。base_url/model 从 env
    (MEM_LLM_BASE_URL/MEM_LLM_MODEL) 读, 默认智谱; key 从 env ZHIPU_API_KEY
    或 CCR config(zhipu-anthropic.api_key)读。"""
    return [ZhipuAnthropicProvider(
        base_url=os.environ.get("MEM_LLM_BASE_URL", "https://open.bigmodel.cn/api/anthropic"),
        model=os.environ.get("MEM_LLM_MODEL", "glm-5-turbo"),
    )]


def _demo() -> None:  # ponytail self-check (mock provider, no network)
    class _Fake:
        base_url = None
        def extract_facts(self, text: str) -> Extraction:
            return Extraction(
                entities=[EntityOut("用户", "person"), EntityOut("rust", "tool")],
                edges=[EdgeOut("用户", "uses", "rust", topic="用户使用 rust")],
                confidence=0.7, source_meta={"provider": "fake"})

    r = extract_facts("用户使用 rust", providers=[_Fake()])
    assert r.edges and r.confidence >= 0.6, (r.edges, r.confidence)
    # entity union dedupe + both endpoints declared
    names = {e.name for e in r.entities}
    assert {"用户", "rust"} <= names, names
    # 无 provider → block (不降级 regex)
    try:
        extract_facts("x", providers=[])
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError on no reachable provider")
    print("adapter ok:", r.edges[0])


if __name__ == "__main__":
    _demo()
