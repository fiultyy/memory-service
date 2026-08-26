"""mem-service llm_extract — LLM 直抽主径通道 (batch 12, 用户指令 2026-08-27).

背景: T2 全量冷启动质量实测 — regex/gazetteer 占位层产出垃圾事实 (虚词
实体「可能/的同时完成」、巨型吸尘器实体「前一次」277 别名吸附 37% active
fact、自环)。用户裁决: regex 前置通道上门禁暂闭 (env ``MEM_EXTRACT_CHANNEL``,
默认 ``llm``), 主径改 LLM 直抽, glm-5-turbo 干净专项封装。

设计落点 (派单 §2 定案):

- **复用** :class:`llm_provider.ZhipuAnthropicProvider` 的 ``chat()`` seam
  (Anthropic Messages 直连, 响亮失败语义); **不复用** adapter 的 wings 投票
  机械 (那是 M4 升级队列消费者, 独立工况不动)。
- **无降级红线 (延续)**: LLM 不可达/坏 JSON 重试仍败 → 抛
  :class:`ExtractFailed` (RuntimeError 子类) → bootstrap 既有 except 记账
  (skip 段 + errors += 1)。**绝不回落 regex** — 本文件不 import extractor,
  grep 断言测试锁死。
- **结构化输出**: 严格 JSON schema 校验 (predicate 12 门枚举 / confidence
  0-1 / subject/object 必须引用已声明实体名或带 value 字面值)。
- **source 不变式**: 只从输入段抽取, evidence 字段强制 (原文 span)。

实体卫生门 (§2.4) 在 autodream 落库 seam (``entity_hygiene_gate``): 停用词/
最小长度/自环/alias cap — regex 通道重开时同样受益 (store 侧防线)。
"""

from __future__ import annotations

import json
import os
from typing import Any

from llm_provider import EdgeOut, EntityOut, Extraction, ProviderCallError

# ── 配置 ─────────────────────────────────────────────────────────────

# predicate 12 门 = extractor.py 高泛化原语集 (v21b 定版)。与 regex 门同一
# 语义边界 (regex 是这套边界的硬编码兜底) — 重开时中英文投影一致。
PREDICATES = frozenset({
    "is_a", "uses", "depends_on", "contains", "belongs_to", "implements",
    "connected_to", "located_in", "causes", "based_on", "prefers", "decided",
})

# 实体 type 枚举 (§2.3 schema v1; 与 extractor 9 类收拢为 5 泛化类)。
ENTITY_TYPES = frozenset({
    "technical_term", "named_entity", "quoted_term", "identifier", "concept",
})

# 抽取通道门禁 (§2.1): llm 默认 | regex 遗留可显式重开。读取函数供测试 pin。
CHANNEL_LLM = "llm"
CHANNEL_REGEX = "regex"


def extract_channel() -> str:
    """当前抽取通道 (env ``MEM_EXTRACT_CHANNEL``, 默认 ``llm``)。

    未知值 → ``llm`` (默认档; 响亮默认优于响亮崩溃 — 门禁拼错重开 regex
    是危险方向, 落回主径安全)。
    """
    v = (os.environ.get("MEM_EXTRACT_CHANNEL") or "").strip().lower()
    return v if v in (CHANNEL_LLM, CHANNEL_REGEX) else CHANNEL_LLM


# ── prompt (资产版本化: docs/llm-extract-prompt.md 是唯一权威文本) ────

PROMPT_VERSION = "v1"

# 单段输入 token 预算 (chars): bootstrap CHUNK=4000 同量级; LLM 通道在
# autodream 段级调用 (段已 ≤ 预算), 此处再设硬顶防 transcript 超长段。
MAX_SEGMENT_CHARS = 4000


def system_prompt() -> str:
    """system prompt 全文 (与 docs/llm-extract-prompt.md v1 逐字一致)。

    版本化: 改 prompt 必须同步改 docs + bump PROMPT_VERSION (资产纪律)。
    """
    return _SYSTEM_PROMPT


_SYSTEM_PROMPT = """你是双语记忆抽取员, 从输入文本段抽取知识图谱实体与事实。只输出纯 JSON, 不要任何解释或 markdown 代码围栏。

## 实体 (entities)
- name: 原文中的专有名词/技术术语/概念原样 (保留大小写/连字符/缩写原形, 如 A2A / pydantic-ai / 护理担保)
- type: 从 [technical_term, named_entity, quoted_term, identifier, concept] 选一个
- aliases: 该实体在文中出现过的其他写法 (另一种语言的形/缩写/大小写变体), 没有空数组
- 中英双语同义实体必须归并为一条: name 取原文主形, aliases 收另一种语言的形 (如 name="aged care guarantee", aliases=["护理担保"])

## 事实 (facts)
- subject: 必须是 entities[].name 里出现过的名字 (原样引用, 不可改写)
- predicate: 从 12 门枚举里选一个:
  is_a(是/属于类别) | uses(使用/采用) | depends_on(依赖/需要) | contains(包含)
  belongs_to(属于/隶属) | implements(实现/落地) | connected_to(连接/对接/集成)
  located_in(位于) | causes(导致/引发) | based_on(基于/借鉴)
  prefers(偏好/首选) | decided(决定采用/选定)
- object: 另一个已声明实体的 name (原样引用); 若原文目标是字面值 (版本号/日期/数值), 用 object 引用最近的已声明实体并在 value 里放字面值
- value: 可选字面值 (str), 仅当原文是字面量陈述 (如 "版本 0.1.9")
- confidence: 0.0-1.0 浮点, 你对这条事实确实在原文中有依据的置信度
- evidence: 原文中支持这条事实的逐字 span (必须从输入段原文复制, 不改写)

## 硬规则
1. 只抽原文有据的事实 — evidence 字段必须能逐字在输入段中找到。禁止用世界知识补全、推断或脑补。
2. 不确定的宁缺毋滥: 没有明确句式依据就不抽。
3. 停用词类虚词/状态词 (可能/的同时完成/前一次/本次/输出/完成/继续 等) 永不作为实体。
4. 自环禁止: subject == object 的事实直接丢弃。
5. 找不到任何实体/事实就输出 {"entities": [], "facts": []}。

## 输出格式 (纯 JSON, 单个对象)
{"entities": [{"name": "...", "type": "...", "aliases": ["..."]}],
 "facts": [{"subject": "...", "predicate": "uses", "object": "...", "value": null, "confidence": 0.9, "evidence": "原文 span"}]}"""


# ── 用户消息模板 (few-shot 内嵌; 语料取 claw 真实段, 见 docs) ────────

_USER_TEMPLATE = """## 示例 1 (中文段)
输入: 2026-08-24 排查 WARP 卡顿: 问题出在 dais 编排循环, 它依赖 logseq-cli 的 node 子进程, 决定采用 pkill 方案兜底。
输出: {{"entities": [{{"name": "dais", "type": "technical_term", "aliases": []}}, {{"name": "logseq-cli", "type": "technical_term", "aliases": []}}, {{"name": "node", "type": "technical_term", "aliases": ["node 子进程"]}}, {{"name": "pkill", "type": "technical_term", "aliases": ["pkill 方案"]}}], "facts": [{{"subject": "dais", "predicate": "depends_on", "object": "logseq-cli", "value": null, "confidence": 0.9, "evidence": "它依赖 logseq-cli 的 node 子进程"}}, {{"subject": "dais", "predicate": "decided", "object": "pkill", "value": null, "confidence": 0.85, "evidence": "决定采用 pkill 方案兜底"}}]}}

## 示例 2 (英文段)
输入: The smart-glasses project uses an Apollo510b MCU; the team prefers waveguide displays over prism optics for the final build.
输出: {{"entities": [{{"name": "smart-glasses project", "type": "named_entity", "aliases": ["智能眼镜项目"]}}, {{"name": "Apollo510b", "type": "technical_term", "aliases": ["Apollo510b MCU"]}}, {{"name": "waveguide display", "type": "technical_term", "aliases": ["waveguide displays"]}}, {{"name": "prism optics", "type": "technical_term", "aliases": []}}], "facts": [{{"subject": "smart-glasses project", "predicate": "uses", "object": "Apollo510b", "value": null, "confidence": 0.95, "evidence": "The smart-glasses project uses an Apollo510b MCU"}}, {{"subject": "smart-glasses project", "predicate": "prefers", "object": "waveguide display", "value": null, "confidence": 0.9, "evidence": "the team prefers waveguide displays over prism optics"}}]}}

## 现在抽取以下输入段
输入: {segment}"""


# ── schema 校验 ──────────────────────────────────────────────────────

class SchemaViolation(RuntimeError):
    """LLM 输出未通过 schema 校验 (携带可读原因, 重试反馈用)。"""


def _clamp01(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise SchemaViolation(f"confidence 非数值: {v!r}")
    return max(0.0, min(1.0, f))


def validate(doc: Any) -> tuple[list[EntityOut], list[EdgeOut], float]:
    """schema v1 校验 + 归一 (§2.3)。

    规则: 顶层 dict / entities[].name 必填 str / type 枚举 (缺省 concept,
    表外值 → concept 收拢) / predicate 枚举 (表外 → SchemaViolation) /
    confidence clamp 0-1 / subject/object 必须引用已声明实体名 / value
    可选 str / evidence 必填非空。返回 (entities, edges, aggregate_conf)。
    违规**整体拒** (不静默丢条 — 那是坏输出混入的口子; 由重试机制整体重试)。
    """
    if not isinstance(doc, dict):
        raise SchemaViolation(f"顶层非对象: {type(doc).__name__}")
    raw_ents = doc.get("entities")
    if raw_ents is None:
        raw_ents = []
    if not isinstance(raw_ents, list):
        raise SchemaViolation("entities 非数组")

    entities: list[EntityOut] = []
    seen_names: set[str] = set()
    for e in raw_ents:
        if not isinstance(e, dict):
            raise SchemaViolation(f"entity 项非对象: {e!r}")
        name = e.get("name")
        if not isinstance(name, str) or not name.strip():
            raise SchemaViolation(f"entity.name 缺失/非 str: {e!r}")
        name = name.strip()
        etype = e.get("type", "concept")
        if etype not in ENTITY_TYPES:
            etype = "concept"  # 表外收拢 (LLM type 漂移不拒整批, 概念档兜底)
        raw_aliases = e.get("aliases") or []
        if not isinstance(raw_aliases, list):
            raise SchemaViolation(f"aliases 非数组: {name!r}")
        aliases = [str(a).strip() for a in raw_aliases
                   if isinstance(a, (str, int, float)) and str(a).strip()]
        if name in seen_names:
            continue  # 同名重复声明 → 保首条 (merge 语义)
        seen_names.add(name)
        entities.append(EntityOut(name=name, type=etype, aliases=aliases))

    raw_facts = doc.get("facts", [])
    if raw_facts is None:
        raw_facts = []
    if not isinstance(raw_facts, list):
        raise SchemaViolation("facts 非数组")
    edges: list[EdgeOut] = []
    confs: list[float] = []
    for f in raw_facts:
        if not isinstance(f, dict):
            raise SchemaViolation(f"fact 项非对象: {f!r}")
        subj = f.get("subject")
        obj = f.get("object")
        pred = f.get("predicate")
        if not (isinstance(subj, str) and isinstance(obj, str)):
            raise SchemaViolation(f"subject/object 非 str: {f!r}")
        subj, obj = subj.strip(), obj.strip()
        if not subj or not obj:
            raise SchemaViolation(f"subject/object 空: {f!r}")
        if pred not in PREDICATES:
            raise SchemaViolation(
                f"predicate 表外: {pred!r} (合法: {sorted(PREDICATES)})")
        if subj not in seen_names:
            raise SchemaViolation(f"subject 未声明: {subj!r}")
        if obj not in seen_names:
            raise SchemaViolation(f"object 未声明: {obj!r}")
        if subj == obj:
            continue  # 自环: schema 层静默弃 (非违规 — 提示已禁, 兜底丢弃)
        conf = _clamp01(f.get("confidence", 0.5))
        value = f.get("value")
        if value is not None and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)  # 数值/bool → str
        evidence = f.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            raise SchemaViolation(f"evidence 缺失/非 str: {subj!r}->{obj!r}")
        edges.append(EdgeOut(subject=subj, predicate=pred, object=obj,
                             topic=evidence.strip(), confidence=conf))
        confs.append(conf)

    aggregate = (sum(confs) / len(confs)) if confs else 0.0
    return entities, edges, aggregate


# ── 主入口 ────────────────────────────────────────────────────────────

class ExtractFailed(RuntimeError):
    """LLM 直抽失败 (网络/坏 JSON 重试仍败)。调用方 (bootstrap) 记
    errors + skip 段 — **绝不静默降级 regex** (用户红线)。"""


def _parse_json_block(content: str) -> Any:
    """剥可能的 ```json 围栏后解析整块 JSON; 失败抛 SchemaViolation。"""
    s = content.strip()
    if s.startswith("```"):
        # ```json\n...\n``` → 取围栏体
        first_nl = s.find("\n")
        if first_nl != -1 and s.rstrip().endswith("```"):
            s = s[first_nl + 1:s.rstrip().rfind("```")]
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        raise SchemaViolation(f"JSON 解析失败: {e}") from e


def extract(segment: str, provider=None) -> Extraction:
    """LLM 直抽一段文本 → ``Extraction`` (autodream 需要的形状)。

    流程: system prompt (版本化资产) + few-shot 用户消息 → provider.chat →
    JSON 解析 → schema 校验。校验失败 → 1 次重试 (附违规原因反馈); 重试
    仍败/网络失败 → 抛 :class:`ExtractFailed` (响亮)。

    Args:
        segment: 段文本 (超 MAX_SEGMENT_CHARS 截断, 与 bootstrap CHUNK 同量级)。
        provider: 任何有 ``chat(system, messages, max_tokens)`` 的对象
            (缺省 :class:`ZhipuAnthropicProvider`; 测试注入 mock)。

    Returns:
        ``Extraction``: entities/edges (EdgeOut.confidence 每边),
        aggregate confidence, source_meta (provider/model/prompt_version/
        retries/evidence 在 EdgeOut.topic)。
    """
    if len(segment) > MAX_SEGMENT_CHARS:
        segment = segment[:MAX_SEGMENT_CHARS]
    if provider is None:
        from llm_provider import ZhipuAnthropicProvider
        provider = ZhipuAnthropicProvider()

    messages: list[dict] = [{"role": "user",
                             "content": _USER_TEMPLATE.format(segment=segment)}]
    retries = 0
    last_err: Exception | None = None
    for attempt in range(2):  # 首次 + 1 重试 (§2.2 落地要求)
        if attempt == 1:
            # 重试反馈: 把违规原因附给模型 (结构化纠错回路)。
            messages = messages[:1] + [
                {"role": "assistant", "content": "(上一轮输出未通过校验)"},
                {"role": "user", "content": (
                    f"你上一轮的输出未通过 schema 校验, 原因: {last_err}。"
                    "请严格按输出格式重新输出纯 JSON, 不要解释。")},
            ]
        try:
            # 输出上限随段长伸缩: 密集大段(月度摘要~2k字)的合法 JSON 可达
            # >1500 tok, 固定默认会在字符串中截断 → 坏 JSON 两轮败(梯度
            # 实测 2026-06/07-summary)。CJK≈1 tok/字, *3 余量, 上限 6000。
            out_cap = max(1500, min(6000, len(segment) * 3))
            content = provider.chat(_SYSTEM_PROMPT, messages,
                                    max_tokens=out_cap)
        except ProviderCallError as e:
            # 网络层失败重试无意义 (同一 provider 会再败; 且「LLM 不可达 =
            # skip」是既定语义) — 直接响亮抛出。
            raise ExtractFailed(f"provider 不可达: {e}") from e
        try:
            doc = _parse_json_block(content)
            entities, edges, aggregate = validate(doc)
            return Extraction(
                entities=entities, edges=edges, confidence=aggregate,
                source_meta={"provider": "zhipu", "extractor_label": "llm",
                             "model": getattr(provider, "model", ""),
                             "prompt_version": PROMPT_VERSION,
                             "retries": retries})
        except SchemaViolation as e:
            last_err = e
            retries += 1
    raise ExtractFailed(
        f"schema 校验两轮失败 (prompt {PROMPT_VERSION}): {last_err}")


__all__ = ["extract", "validate", "extract_channel", "ExtractFailed",
           "SchemaViolation", "PREDICATES", "ENTITY_TYPES", "PROMPT_VERSION",
           "CHANNEL_LLM", "CHANNEL_REGEX", "MAX_SEGMENT_CHARS"]
