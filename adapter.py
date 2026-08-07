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

from llm_provider import (
    Extraction,
    FactOut,
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
      triples (quorum ⌈n/2⌉), aggregate confidence as the max.
    - Empty vote **with provider errors** ⇒ ``RuntimeError`` (LLM unavailable:
      no key / network / parse). Empty vote **without errors** ⇒ returned
      as-is (the LLM legitimately found no fact).
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
    if not voted.facts:
        errs = [e.source_meta.get("error") for e in extractions
                if e.source_meta.get("error")]
        if errs:
            raise RuntimeError(
                f"LLM providers returned no facts (errors: {errs[:2]}). "
                "regex fallback removed — block instead of silent low-quality ingest.")
    return voted


# ── voting / aggregation ──────────────────────────────────────────────

def _vote(extractions: list[Extraction]) -> Extraction:
    """Majority vote per (subject, predicate, object) triple; confidence = max.

    case-fold 投票 key (A2A/a2a 合并达 quorum), 但 surviving 保留原 FactOut
    (大小写原样存 KG)。ponytail: 最浅归一 (case-fold only), 不做 lemmatize/
    alias (upgrade path)。A triple survives if it appears in ≥ ⌈n/2⌉ wings.
    """
    n = len(extractions)
    quorum = (n + 1) // 2  # ⌈n/2⌉: 3→2, 2→1, 1→1
    triple_wings: dict[tuple[str, str, str], list[tuple[int, FactOut]]] = {}
    for wi, ext in enumerate(extractions):
        for f in ext.facts:
            key = (f.subject.strip().lower(), f.predicate.strip().lower(),
                   f.object.strip().lower())
            triple_wings.setdefault(key, []).append((wi, f))

    surviving: list[FactOut] = []
    contributing_confidences: list[float] = []
    agree_hist: list[int] = []
    for key, wing_facts in triple_wings.items():
        agree_hist.append(len(wing_facts))
        if len(wing_facts) >= quorum:
            surviving.append(wing_facts[0][1])  # 保留首个原 FactOut (大小写原样)
            for wi, _ in wing_facts:
                contributing_confidences.append(extractions[wi].confidence)

    conf = max(contributing_confidences) if contributing_confidences else 0.0
    return Extraction(
        facts=surviving, confidence=conf,
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
            return Extraction(facts=[FactOut("用户", "uses", "rust")],
                              confidence=0.7, source_meta={"provider": "fake"})

    r = extract_facts("用户使用 rust", providers=[_Fake()])
    assert r.facts and r.confidence >= 0.6, (r.facts, r.confidence)
    # 无 provider → block (不降级 regex)
    try:
        extract_facts("x", providers=[])
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError on no reachable provider")
    print("adapter ok:", r.facts[0])


if __name__ == "__main__":
    _demo()
